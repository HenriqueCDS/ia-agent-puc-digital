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

from app.api.ratelimit import RateLimitExcedido
from app.api.schemas import ErroOut
from app.providers.chain import ModeloInvalido

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


class RecursoNaoEncontrado(Exception):
    """Um `id` de rota que não existe (ver `routers/perguntas.py`). 404 no
    envelope padrão, em vez do `{"detail": ...}` cru da HTTPException do
    Starlette — quem integra lê `erro` do mesmo jeito que nos outros erros."""


class SemPermissao(Exception):
    """Consumidor autenticado, mas sem direito a esta operação (ver
    `deps.consumidor_de_escrita_de_avaliacao`).

    403 e não 401: 401 diria "sua credencial não vale, tente outra"; aqui a
    credencial vale, o consumidor foi identificado — ele só não pode. O CRUD
    de escrita de `/v1/perguntas` usa isto para barrar toda integração que não
    seja a de avaliação (a da demo, por exemplo, cuja chave é pública no HTML).
    """


class NaoAutenticado(Exception):
    """Chave de API ausente ou inválida (ver `deps.get_consumidor`).

    401 e não 403: a diferença importa para quem integra. 401 diz "identifique-se
    / sua credencial não vale" — reenviar com outra chave pode funcionar. 403
    diria "identificamos você e você não pode", que não é o caso aqui.

    A mensagem NUNCA diz se a chave existe mas está errada ou se não existe:
    isso transformaria o endpoint num oráculo para enumerar chaves válidas.
    """


def _envelope(
    request: Request,
    status: int,
    erro: str,
    detalhe: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    corpo = ErroOut(erro=erro, detalhe=detalhe, request_id=request_id)
    return JSONResponse(status_code=status, content=corpo.model_dump(), headers=headers)


def registrar_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validacao(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Schema do Pydantic (ex.: T1.6 min_length/max_length) — mesma família
        # 422 da validação semântica abaixo, mesmo envelope.
        return _envelope(request, 422, "validacao", str(exc.errors()))

    @app.exception_handler(AssuntoInvalido)
    async def _assunto_invalido(request: Request, exc: AssuntoInvalido) -> JSONResponse:
        return _envelope(request, 422, "assunto_invalido", str(exc))

    @app.exception_handler(ModeloInvalido)
    async def _modelo_invalido(request: Request, exc: ModeloInvalido) -> JSONResponse:
        # Handler próprio, e não o `ValueError` genérico abaixo: `erro` é a
        # chave estável que quem integra lê programaticamente, e devolver
        # "pergunta_invalida" para um `modelo` errado mandaria o cliente
        # reescrever a pergunta — o único campo que estava certo.
        return _envelope(request, 422, "modelo_invalido", str(exc))

    @app.exception_handler(ValueError)
    async def _valor_invalido(request: Request, exc: ValueError) -> JSONResponse:
        # Ex.: pergunta vazia (app/agent/preprocess.normalize). AssuntoInvalido
        # já foi pego pelo handler mais específico acima.
        return _envelope(request, 422, "pergunta_invalida", str(exc))

    @app.exception_handler(SemPermissao)
    async def _sem_permissao(request: Request, exc: SemPermissao) -> JSONResponse:
        return _envelope(request, 403, "sem_permissao", str(exc))

    @app.exception_handler(RecursoNaoEncontrado)
    async def _nao_encontrado(request: Request, exc: RecursoNaoEncontrado) -> JSONResponse:
        return _envelope(request, 404, "nao_encontrado", str(exc))

    @app.exception_handler(NaoAutenticado)
    async def _nao_autenticado(request: Request, exc: NaoAutenticado) -> JSONResponse:
        # `WWW-Authenticate` não se aplica: o esquema é uma chave em header
        # próprio, não HTTP Authentication — anunciar `Basic`/`Bearer` faria o
        # navegador abrir a caixa de login errada.
        return _envelope(request, 401, "nao_autenticado", str(exc))

    @app.exception_handler(RateLimitExcedido)
    async def _rate_limit(request: Request, exc: RateLimitExcedido) -> JSONResponse:
        # `Retry-After` é o que separa um cliente que espera do que entra em
        # loop de retry e piora o congestionamento que causou o 429.
        return _envelope(
            request,
            429,
            "rate_limit",
            str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(NotImplementedError)
    async def _nao_suportado(request: Request, exc: NotImplementedError) -> JSONResponse:
        # Hoje só anexos (app/agent/preprocess.normalize) — v1 não interpreta imagem.
        return _envelope(request, 501, "recurso_nao_suportado", str(exc))

    @app.exception_handler(RuntimeError)
    async def _config_ausente(request: Request, exc: RuntimeError) -> JSONResponse:
        # Dois casos, mesma resposta: nenhuma chave de LLM configurada (cadeia
        # vazia, `ProviderChain.__init__`) e todos os providers indisponíveis
        # (`base.TodosProvidersFalharam`, que é `RuntimeError` justamente para
        # cair aqui). 503, não 500: é o serviço indisponível, não o pedido
        # errado — e é o código que diz a quem integra que vale tentar de novo.
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
