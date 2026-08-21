"""Contratos HTTP da API (Pydantic). O núcleo do agente (`app/core/models.py`)
não conhece HTTP — é aqui que `Query`/`Answer`/`RetrievedChunk` viram JSON."""

from typing import Literal

from pydantic import BaseModel, Field

from app.core.models import Answer, Origem, RetrievedChunk


class AskRequest(BaseModel):
    # `min_length=3`: rejeita antes de virar prompt — sem isso, um payload de
    # 2 MB percorre normalize() -> retrieve() -> LLM antes de qualquer coisa
    # reclamar, e você paga tokens de entrada por lixo.
    pergunta: str = Field(min_length=3, max_length=1000)
    assunto: str | None = None


class SourceOut(BaseModel):
    titulo: str
    tipo: Literal["documento", "web"]
    # None para "documento": não existe endpoint que sirva o PDF, e expor o
    # `file://` local da máquina vazaria caminho de disco para quem chama a API.
    # Para "web" é a própria URL pública — é o dado que T1.3 existe para não
    # jogar fora (hoje `fontes: list[str]` o descarta).
    url: str | None = None
    pagina: int | None = None
    score: float


class AskResponse(BaseModel):
    resposta: str
    fontes: list[SourceOut]
    encontrou_na_base: bool
    # Importado de `app/core/models`, nunca reescrito aqui — ver o comentário
    # de `Origem` lá. Um valor novo de origem no agente entra neste contrato
    # sozinho, em vez de virar literal_error na resposta.
    origem: Origem
    request_id: str


class AssuntosOut(BaseModel):
    assuntos: list[str]


class ProntidaoOut(BaseModel):
    """Resposta de `/v1/ready` (T2.6). `checagens` diz QUAL dependência caiu —
    sem isso, um 503 obriga quem opera a ir no log da app para saber se o
    problema é o Postgres ou a chave do LLM."""

    status: Literal["ok", "indisponivel"]
    checagens: dict[str, bool]


class ErroOut(BaseModel):
    """Envelope único para toda resposta de erro — ver app/api/errors.py.

    `erro` é a chave estável para quem integra decidir programaticamente (não
    precisa fazer parsing de `detalhe`, que é só para humano lendo o log)."""

    erro: str
    detalhe: str
    request_id: str | None = None


def _fonte_para_source_out(chunk: RetrievedChunk) -> SourceOut:
    meta = chunk.document.metadata
    pagina = meta.get("page")
    if meta.get("origem") == "web":
        return SourceOut(
            titulo=meta.get("titulo") or meta.get("source_name", ""),
            tipo="web",
            url=meta.get("source_name"),
            score=chunk.score,
        )
    return SourceOut(
        titulo=meta.get("source_name", "desconhecido"),
        tipo="documento",
        pagina=(pagina + 1) if pagina is not None else None,
        score=chunk.score,
    )


def ask_response_de(resultado: Answer, request_id: str) -> AskResponse:
    return AskResponse(
        resposta=resultado.text,
        fontes=[_fonte_para_source_out(c) for c in resultado.sources],
        encontrou_na_base=resultado.grounded,
        origem=resultado.origem,
        request_id=request_id,
    )
