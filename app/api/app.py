"""Monta a aplicação FastAPI. `app/main.py` só chama `create_app()`."""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import registrar_handlers
from app.api.routers.v1 import router as v1_router
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


def create_app() -> FastAPI:
    telemetry.configurar_logs()
    telemetry_store.habilitar()

    app = FastAPI(
        title="Agente de Suporte Acadêmico",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.add_middleware(_RequestIdMiddleware)
    registrar_handlers(app)
    app.include_router(v1_router)

    return app
