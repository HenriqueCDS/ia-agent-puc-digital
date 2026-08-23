"""Monta a aplicação FastAPI. `app/main.py` só chama `create_app()`."""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import registrar_handlers
from app.api.ratelimit import reset_rate_limiter
from app.api.routers.demo import IMAGENS as demo_imagens
from app.api.routers.demo import router as demo_router
from app.api.routers.v1 import router as v1_router
from app.core.config import settings
from app.core import telemetry
from app.db import telemetry_store
from app.db.vector_store import get_vector_store
from app.providers.gemini import get_embeddings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Warm-up no boot, não na 1ª request.

    `get_embeddings()` carrega ~1GB de pesos do disco na primeira vez que é
    chamado — sem isto, o primeiro `/v1/ask` depois do deploy paga esse custo
    (medido: até 65s neste projeto). Feito aqui, o custo vira parte do boot do
    servidor, que já é esperado, e não da experiência do primeiro aluno.
    `SELECT 1` prova a conexão com o Postgres pela mesma razão: falhar agora,
    de forma visível no boot, é melhor que falhar silenciosamente na 1ª pergunta.
    """
    logger.info("warm-up: carregando modelo de embeddings...")
    get_embeddings()

    store = get_vector_store()
    with store.session_maker() as session:
        session.execute(text("SELECT 1"))
    logger.info("warm-up: concluído, conexão com o Postgres confirmada.")

    yield


class _RequestIdMiddleware:
    """Preenche `request.state.request_id` e o header `X-Request-Id`.

    Correlaciona uma reclamação pontual ("recebi resposta errada às 14h32")
    com a linha exata da tabela `telemetria` — hoje não há como fazer essa
    ponte.

    ASGI puro, não `@app.middleware("http")` (que é `BaseHTTPMiddleware`) —
    esse decorator tem uma interação conhecida do Starlette em que a exceção
    original é relançada de volta ao chamador mesmo depois de um exception
    handler (ver `errors.py`) já ter tratado e devolvido uma resposta,
    fazendo o 500/503/etc que deveríamos devolver limpo estourar de novo.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def _send_com_header(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        await self.app(scope, receive, _send_com_header)


def _configurar_cors(app: FastAPI) -> None:
    """CORS só para as origens declaradas no .env (T2.2).

    Sem origem configurada, o middleware NÃO entra: a ausência de cabeçalho
    CORS é o que já bloqueia a chamada cruzada no navegador, e a demo servida
    pela própria API (T3.1) é mesma origem — não precisa de nada disto.

    `allow_credentials=False` de propósito: a autenticação daqui é uma chave em
    header, não cookie de sessão. Ligar credentials sem precisar é o que torna
    um CORS mal configurado explorável de verdade. `*` é filtrado antes, em
    `settings.cors_origins_lista`.
    """
    origens = settings.cors_origins_lista
    if not origens:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origens,
        allow_credentials=False,
        allow_methods=["GET", "POST"],  # o que a v1 expõe; não abrir o resto
        allow_headers=["Content-Type", "X-API-Key"],
        expose_headers=["X-Request-Id", "Retry-After"],  # inúteis se o JS não puder lê-los
    )
    logger.info("CORS habilitado para: %s", ", ".join(origens))


def _conferir_chave_da_demo() -> None:
    """Avisa no BOOT se a demo não tem chave — não na primeira pergunta do aluno.

    Sem uma integração com o nome de `DEMO_CONSUMIDOR` em `API_KEYS`, a página
    carrega normalmente e só o `fetch` toma 401: o sintoma aparece longe da
    causa (uma linha faltando no `.env`). Não é fatal de propósito — a v1
    continua servindo o AVA, que é o que não pode cair por causa da demo.
    """
    if not settings.api_auth_enabled:
        logger.warning(
            "demo em /demo com API_AUTH_ENABLED=false: a API está aberta. "
            "Aceitável só em desenvolvimento local."
        )
        return

    if settings.chave_do_consumidor(settings.demo_consumidor) is None:
        logger.error(
            "demo habilitada mas API_KEYS não tem a integração '%s': /demo vai "
            "carregar e todo /v1/ask dela vai tomar 401. Cadastre "
            "`%s:<chave>` em API_KEYS ou desligue com DEMO_ENABLED=false.",
            settings.demo_consumidor,
            settings.demo_consumidor,
        )
        return

    if settings.demo_consumidor not in settings.tetos_diarios_por_consumidor:
        logger.warning(
            "a integração '%s' não tem teto diário próprio "
            "(RATE_LIMIT_DIARIO_POR_CONSUMIDOR): a chave da demo é pública no "
            "HTML e hoje responde ao teto global, o mesmo do AVA.",
            settings.demo_consumidor,
        )


def create_app() -> FastAPI:
    telemetry.configurar_logs()
    telemetry_store.habilitar()

    app = FastAPI(
        title="Agente de Suporte Acadêmico",
        version="1.0.0",
        lifespan=_lifespan,
    )

    _configurar_cors(app)
    app.add_middleware(_RequestIdMiddleware)
    registrar_handlers(app)
    app.include_router(v1_router)
    if settings.demo_enabled:
        app.include_router(demo_router)
        # A identidade visual da demo (logo institucional). Mesma origem, então
        # a página continua sem depender de nenhum host externo — que é o que
        # permite abri-la numa máquina sem internet.
        app.mount("/static", StaticFiles(directory=demo_imagens), name="static")
        _conferir_chave_da_demo()

    # Contadores de rate limit são por processo (ver ratelimit.py). Dois apps
    # criados no mesmo processo — o que só acontece em teste — não podem herdar
    # o balde um do outro, senão a ordem dos testes muda o resultado.
    reset_rate_limiter()

    if settings.api_auth_enabled and not settings.api_keys_por_chave:
        # Visível no boot, não na primeira request: as rotas protegidas vão
        # responder 503 (ver deps.get_consumidor) e o motivo precisa estar no
        # log antes de alguém abrir um chamado.
        logger.error(
            "API_AUTH_ENABLED=true e nenhuma chave em API_KEYS: /v1/ask e "
            "/v1/assuntos vão responder 503 até o .env ser corrigido."
        )

    return app
