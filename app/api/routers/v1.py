"""Rotas v1. Só orquestra HTTP — a lógica inteira mora em app/agent/responder."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.agent.responder import answer
from app.api.deps import consumidor_com_rate_limit, get_assuntos_validos, get_consumidor, get_request_id
from app.api.errors import AssuntoInvalido
from app.api.schemas import (
    AskRequest,
    AskResponse,
    AssuntosOut,
    CatalogoOut,
    ModelosOut,
    ProntidaoOut,
    ProviderOut,
    ask_response_de,
)
from app.core import telemetry
from app.core.config import settings
from app.core.models import Query
from app.db.vector_store import get_vector_store
from app.providers.chain import (
    ModeloInvalido,
    chaves_por_provider,
    listar_modelos_cache,
    modelo_padrao,
    sem_segredo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/health")
def health() -> dict[str, str]:
    """LIVENESS: o processo está de pé e respondendo. Nada mais.

    Público e sem nenhuma checagem de dependência de propósito. É o que um
    orquestrador usa para decidir se REINICIA o container — e reiniciar a app
    porque o Postgres caiu não conserta o Postgres, só derruba também as
    respostas que ainda saíam do cache. Quem quer saber se dá para atender
    tráfego chama `/ready`.
    """
    return {"status": "ok"}


@router.get("/ready", response_model=ProntidaoOut)
def ready() -> JSONResponse:
    """READINESS: dá para atender uma pergunta agora?

    Checa o que `/ask` precisa e a app não controla: Postgres (retrieval e
    cache) e a chave do Gemini. Até T2.6 só existia `/health`, que respondia
    "ok" incondicionalmente — com o banco derrubado o monitoramento continuava
    verde enquanto todo `/ask` devolvia 503.

    Público como o `/health`: o corpo diz "banco fora do ar", nunca por quê nem
    onde. Exigir chave aqui impediria justamente quem monitora de monitorar.

    503 (e não 200 com um campo `pronto: false`) porque é assim que probe de
    orquestrador, load balancer e monitoramento leem o resultado — sem parser.
    """
    checagens: dict[str, bool] = {}

    try:
        with get_vector_store().session_maker() as sessao:
            sessao.execute(text("SELECT 1"))
        checagens["banco"] = True
    except Exception as exc:  # OperationalError e qualquer falha de driver/DNS
        logger.error("/ready: banco indisponível: %s", exc)
        checagens["banco"] = False

    # Só a presença: validar a chave de verdade custaria uma chamada paga a cada
    # probe. A chave inválida aparece como 503 no `/ask`.
    #
    # UM provider com chave já é pronto, não os três: a cadeia de fallback
    # existe justamente para o serviço atender com o que tiver de pé (ver
    # app/providers/chain.py). Exigir os três aqui faria a instalação normal —
    # a que só configurou o Gemini — aparecer como indisponível para o
    # monitoramento enquanto responde perfeitamente.
    chaves = chaves_por_provider()
    checagens["chave_llm"] = any(chaves.values())
    # Detalhe por provider, para o operador saber QUAL rede de segurança ele
    # está sem. Fora de `checagens` de propósito: `pronto` é o `all()` do dict,
    # e um OpenRouter não configurado não pode derrubar a prontidão.
    if not all(chaves.values()):
        logger.info(
            "/ready: providers sem chave: %s",
            ", ".join(nome for nome, tem in chaves.items() if not tem) or "nenhum",
        )

    pronto = all(checagens.values())
    corpo = ProntidaoOut(status="ok" if pronto else "indisponivel", checagens=checagens)
    return JSONResponse(status_code=200 if pronto else 503, content=corpo.model_dump())


@router.get("/assuntos", response_model=AssuntosOut)
def assuntos(
    # Autenticação ANTES de `get_assuntos_validos`: o FastAPI resolve as
    # dependências na ordem da assinatura, e sem chave válida a request não
    # deve nem abrir conexão com o banco.
    consumidor: str = Depends(get_consumidor),
    validos: list[str] = Depends(get_assuntos_validos),
) -> AssuntosOut:
    # Autenticada, mas fora do rate limit: é um DISTINCT barato que a demo
    # chama a cada carga de página (ver `deps.consumidor_com_rate_limit`).
    return AssuntosOut(assuntos=validos)


@router.get("/modelos", response_model=ModelosOut)
def modelos(
    consumidor: str = Depends(get_consumidor),
) -> ModelosOut:
    """O que a demo precisa para montar o seletor de modelo, sem chamar
    nenhum provedor: só os defaults do `.env` e se o override está ligado.

    Fora do rate limit, mesma razão de `/assuntos` — é metadado de
    configuração, não uma pergunta que gasta orçamento de LLM.

    Só lista providers que TÊM chave: um sem chave não pode ser escolhido de
    qualquer forma (`cadeia_para_modelo` devolveria `ModeloInvalido`), então
    oferecê-lo no seletor só produziria erro depois de o aluno já ter clicado.
    """
    chaves = chaves_por_provider()
    providers = [
        ProviderOut(provider=nome, modelo_padrao=modelo_padrao(nome))
        for nome in settings.llm_providers_lista
        if chaves.get(nome)
    ]
    return ModelosOut(override_enabled=settings.ask_modelo_override_enabled, providers=providers)


@router.get("/modelos/{provider}", response_model=CatalogoOut)
def catalogo_modelos(
    provider: str,
    consumidor: str = Depends(get_consumidor),
) -> CatalogoOut:
    """Catálogo completo de UM provider — sob demanda, e só quando o override
    está ligado (a mesma checagem de `/ask`: sem isso, escolher um modelo da
    lista sempre terminaria em 422).

    `ModeloInvalido` reaproveita o handler 422 que `/ask` já usa: provider
    desconhecido, sem chave, ou falha ao listar são todos "o pedido não pode
    ser atendido assim", não "o serviço caiu".
    """
    if not settings.ask_modelo_override_enabled:
        raise ModeloInvalido(
            "Escolha de modelo por requisição está desabilitada neste ambiente "
            "(ASK_MODELO_OVERRIDE_ENABLED=false)."
        )
    try:
        modelos_do_provider = listar_modelos_cache(provider.strip().casefold())
    except NotImplementedError:
        raise ModeloInvalido(f"Provider {provider!r} não expõe catálogo de modelos.") from None
    except ModeloInvalido:
        raise
    except Exception as exc:
        # `sem_segredo`: erro de SDK ecoa credencial com frequência, e isto vai
        # direto para a resposta HTTP — a defesa do log não se aplica aqui.
        raise ModeloInvalido(
            f"Falha ao listar modelos de {provider!r}: {sem_segredo(str(exc))}"
        ) from None
    return CatalogoOut(provider=provider, modelos=list(modelos_do_provider))


@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    request_id: str = Depends(get_request_id),
    # Mesma razão de `/assuntos`: autenticação e rate limit primeiro, banco
    # depois. Quem estourou o limite não deve custar uma query.
    consumidor: str = Depends(consumidor_com_rate_limit),
    assuntos_validos: list[str] = Depends(get_assuntos_validos),
) -> AskResponse:
    # O nome da integração (não a chave) vira o `canal` da telemetria: é o que
    # permite atribuir custo por consumidor, que é metade da razão de T2.1.
    telemetry.set_canal(f"api:{consumidor}")
    # T3.2 — o mesmo id que sai no header e no corpo entra no registro. Aqui, e
    # não num middleware: a telemetria é aberta dentro de `answer()`, e é este o
    # ponto em que o id já existe e o contexto ainda é o desta request.
    telemetry.set_request_id(request_id)

    # Hoje um assunto inválido não dá erro: passa o filtro $eq do retrieval,
    # devolve zero chunks e cai silenciosamente no fallback web — falha
    # invisível que parece funcionamento normal. Barrado aqui, antes de
    # `answer()` gastar retrieval (e possivelmente busca web) à toa.
    if req.assunto is not None and req.assunto not in assuntos_validos:
        raise AssuntoInvalido(req.assunto, assuntos_validos)

    # Rejeitar, e não ignorar: com o switch desligado, quem mandou `modelo`
    # está comparando modelos e receberia a resposta do modelo de sempre
    # achando que testou outro — falha silenciosa que produz conclusão errada.
    if req.modelo and not settings.ask_modelo_override_enabled:
        raise ModeloInvalido(
            "Escolha de modelo por requisição está desabilitada neste ambiente "
            "(ASK_MODELO_OVERRIDE_ENABLED=false)."
        )
    if req.modelo:
        # WARNING: é chamada fora do modelo homologado, feita com a chave da
        # instituição. Tem que ser rastreável a quem pediu — a telemetria grava
        # o modelo que respondeu, mas é esta linha que amarra ao consumidor.
        logger.warning(
            "consumidor %s pediu override de modelo: %r (sem fallback)", consumidor, req.modelo
        )

    resultado = answer(Query(text=req.pergunta, assunto=req.assunto, modelo=req.modelo))
    return ask_response_de(resultado, request_id)
