"""Envelope de erro único + exception handlers globais.

Sem isto, hoje só `ValueError` (pergunta vazia) vira 400 — Postgres fora do
ar, `GOOGLE_API_KEY` ausente e anexo não suportado sobem sem tratamento e
viram 500 genérico. Quem integra não consegue distinguir "sua pergunta é
inválida" (não adianta repetir) de "o serviço caiu" (vale fazer retry).
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api.schemas import ErroOut

logger = logging.getLogger(__name__)


class AssuntoInvalido(ValueError):
    """`assunto` que não existe na coleção ativa (ver `deps.get_assuntos_validos`).

    Subclasse de `ValueError` de propósito: cai no mesmo handler (422) da
    "pergunta vazia" sem precisar de um segundo registro — só troca o texto.
    """

    def __init__(self, assunto: str, validos: list[str]):
        self.assunto = assunto
        self.validos = validos
        opcoes = ", ".join(validos) if validos else "(nenhum assunto indexado ainda)"
        super().__init__(f'Assunto "{assunto}" não existe. Válidos: {opcoes}')


def _envelope(request: Request, status: int, erro: str, detalhe: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    corpo = ErroOut(erro=erro, detalhe=detalhe, request_id=request_id)
    return JSONResponse(status_code=status, content=corpo.model_dump())


def registrar_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validacao(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Schema do Pydantic (ex.: T1.6 min_length/max_length) — mesma família
        # 422 da validação semântica abaixo, mesmo envelope.
        return _envelope(request, 422, "validacao", str(exc.errors()))

    @app.exception_handler(AssuntoInvalido)
    async def _assunto_invalido(request: Request, exc: AssuntoInvalido) -> JSONResponse:
        return _envelope(request, 422, "assunto_invalido", str(exc))

    @app.exception_handler(ValueError)
    async def _valor_invalido(request: Request, exc: ValueError) -> JSONResponse:
        # Ex.: pergunta vazia (app/agent/preprocess.normalize). AssuntoInvalido
        # já foi pego pelo handler mais específico acima.
        return _envelope(request, 422, "pergunta_invalida", str(exc))

    @app.exception_handler(NotImplementedError)
    async def _nao_suportado(request: Request, exc: NotImplementedError) -> JSONResponse:
        # Hoje só anexos (app/agent/preprocess.normalize) — v1 não interpreta imagem.
        return _envelope(request, 501, "recurso_nao_suportado", str(exc))

    @app.exception_handler(RuntimeError)
    async def _config_ausente(request: Request, exc: RuntimeError) -> JSONResponse:
        # `_require_api_key` em app/providers/gemini.py: GOOGLE_API_KEY não
        # configurada. 503, não 500: é o serviço indisponível, não o pedido errado.
        return _envelope(request, 503, "servico_indisponivel", str(exc))

    @app.exception_handler(OperationalError)
    async def _banco_indisponivel(request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("banco indisponível: %s", exc)
        return _envelope(request, 503, "banco_indisponivel", "Banco de dados indisponível.")

    @app.exception_handler(Exception)
    async def _erro_interno(request: Request, exc: Exception) -> JSONResponse:
        # Único ponto que não expõe `str(exc)` ao cliente: é o caso não mapeado,
        # então a mensagem pode conter detalhe interno. Trace completo só no log.
        logger.exception("erro não tratado")
        return _envelope(request, 500, "erro_interno", "Erro interno.")
