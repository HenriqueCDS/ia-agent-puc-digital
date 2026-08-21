"""Rotas v1. Só orquestra HTTP — a lógica inteira mora em app/agent/responder."""

from fastapi import APIRouter, Depends

from app.agent.responder import answer
from app.api.deps import get_assuntos_validos, get_request_id
from app.api.errors import AssuntoInvalido
from app.api.schemas import AskRequest, AskResponse, AssuntosOut, ask_response_de
from app.core import telemetry
from app.core.models import Query

router = APIRouter(prefix="/v1")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/assuntos", response_model=AssuntosOut)
def assuntos(validos: list[str] = Depends(get_assuntos_validos)) -> AssuntosOut:
    return AssuntosOut(assuntos=validos)


@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    request_id: str = Depends(get_request_id),
    assuntos_validos: list[str] = Depends(get_assuntos_validos),
) -> AskResponse:
    telemetry.set_canal("api")

    # Hoje um assunto inválido não dá erro: passa o filtro $eq do retrieval,
    # devolve zero chunks e cai silenciosamente no fallback web — falha
    # invisível que parece funcionamento normal. Barrado aqui, antes de
    # `answer()` gastar retrieval (e possivelmente busca web) à toa.
    if req.assunto is not None and req.assunto not in assuntos_validos:
        raise AssuntoInvalido(req.assunto, assuntos_validos)

    resultado = answer(Query(text=req.pergunta, assunto=req.assunto))
    return ask_response_de(resultado, request_id)
