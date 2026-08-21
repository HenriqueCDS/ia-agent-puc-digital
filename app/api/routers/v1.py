"""Rotas v1. Só orquestra HTTP — a lógica inteira mora em app/agent/responder."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.agent.responder import answer
from app.api.deps import consumidor_com_rate_limit, get_assuntos_validos, get_consumidor, get_request_id
from app.api.errors import AssuntoInvalido
from app.api.schemas import AskRequest, AskResponse, AssuntosOut, ProntidaoOut, ask_response_de
from app.core import telemetry
from app.core.config import settings
from app.core.models import Query
from app.db.vector_store import get_vector_store

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

    # Só a presença: validar a chave de verdade custaria uma chamada paga ao
    # Gemini a cada probe. A chave inválida aparece como 503 no `/ask`.
    checagens["chave_llm"] = bool(settings.google_api_key)

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

    # Hoje um assunto inválido não dá erro: passa o filtro $eq do retrieval,
    # devolve zero chunks e cai silenciosamente no fallback web — falha
    # invisível que parece funcionamento normal. Barrado aqui, antes de
    # `answer()` gastar retrieval (e possivelmente busca web) à toa.
    if req.assunto is not None and req.assunto not in assuntos_validos:
        raise AssuntoInvalido(req.assunto, assuntos_validos)

    resultado = answer(Query(text=req.pergunta, assunto=req.assunto))
    return ask_response_de(resultado, request_id)
