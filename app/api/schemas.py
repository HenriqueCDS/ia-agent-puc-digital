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
    # Override do modelo, `[provider:]modelo` (ex.: `groq:llama-3.1-8b-instant`).
    # OPCIONAL e ignorável: quem não manda o campo tem o comportamento de sempre
    # — a cadeia com fallback —, então o contrato de `/ask` continua compatível
    # com quem já integra. Por padrão não usa cache (repetir a mesma pergunta
    # com o mesmo override chama o LLM de novo); `MODELO_OVERRIDE_CACHE_ENABLED`
    # liga o cache também para ele — o modelo entra na chave, então não mistura
    # com outro modelo nem com a cadeia normal.
    #
    # Só é aceito com `ASK_MODELO_OVERRIDE_ENABLED=true`; com o switch desligado
    # (o padrão) a requisição é rejeitada em vez de o campo ser silenciosamente
    # ignorado. Ignorar seria pior: quem está comparando modelos receberia a
    # resposta do modelo de sempre achando que testou outro.
    #
    # `max_length` porque isto vira o `model` de uma chamada externa — sem
    # limite, é um campo de texto livre indo para o provedor.
    modelo: str | None = Field(default=None, max_length=120)


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
    # A resposta veio da `resposta_cache` — nenhuma chamada paga ao LLM. É o
    # dado que a demo (T3.1) mostra ao lado da latência: sem ele, um cache hit
    # de 20ms é indistinguível de "o servidor estava rápido hoje".
    cache_hit: bool
    request_id: str


class AssuntosOut(BaseModel):
    assuntos: list[str]


class ProviderOut(BaseModel):
    """Um provider da cadeia, para o seletor de modelo da demo (`GET /v1/modelos`)."""

    provider: str
    modelo_padrao: str


class ModelosOut(BaseModel):
    """Resposta de `GET /v1/modelos`: o que a demo precisa para montar o seletor
    sem chamar nenhum provedor — só os defaults do `.env` e o feature flag.

    `override_enabled` espelha `ASK_MODELO_OVERRIDE_ENABLED`: o frontend usa
    para decidir se mostra o seletor. Sem isso, a demo ofereceria um controle
    que sempre devolveria 422 — pior que não ter o controle."""

    override_enabled: bool
    providers: list[ProviderOut]


class CatalogoOut(BaseModel):
    """Resposta de `GET /v1/modelos/{provider}`: o catálogo completo, buscado
    sob demanda (ver `providers/chain.listar_modelos_cache`)."""

    provider: str
    modelos: list[str]


class ProntidaoOut(BaseModel):
    """Resposta de `/v1/ready` (T2.6). `checagens` diz QUAL dependência caiu —
    sem isso, um 503 obriga quem opera a ir no log da app para saber se o
    problema é o Postgres ou a chave do LLM."""

    status: Literal["ok", "indisponivel"]
    checagens: dict[str, bool]


class PerguntaOut(BaseModel):
    """Um item do dataset de avaliação (`GET /v1/perguntas`).

    `pergunta_hash` sai no contrato de propósito: é a chave de junção com a
    telemetria (que nunca guarda o texto da pergunta), e a tela de revisão
    precisa dele para casar pergunta × execução."""

    id: int
    grupo: str
    pergunta: str
    pergunta_hash: str
    assunto: str | None
    origem_esperada: Origem
    origem_tambem_ok: list[Origem]
    criterio: str | None
    ativo: bool


class PerguntaCreate(BaseModel):
    pergunta: str = Field(min_length=3, max_length=1000)
    origem_esperada: Origem
    grupo: str = Field(default="", max_length=60)
    assunto: str | None = Field(default=None, max_length=60)
    # `nenhuma` e `encaminhado` dão a mesma mensagem ao aluno — ver
    # `eval_run._origens_aceitas`. Lista, não escalar: uma pergunta pode aceitar
    # mais de uma origem alternativa.
    origem_tambem_ok: list[Origem] = Field(default_factory=list)
    criterio: str | None = Field(default=None, max_length=2000)


class PerguntaUpdate(BaseModel):
    """PATCH: só o que vier é alterado. `ativo=false` é o delete lógico;
    `ativo=true` reativa."""

    pergunta: str | None = Field(default=None, min_length=3, max_length=1000)
    origem_esperada: Origem | None = None
    grupo: str | None = Field(default=None, max_length=60)
    assunto: str | None = Field(default=None, max_length=60)
    origem_tambem_ok: list[Origem] | None = None
    criterio: str | None = Field(default=None, max_length=2000)
    ativo: bool | None = None


class PerguntasListOut(BaseModel):
    perguntas: list[PerguntaOut]
    total: int


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


def pergunta_out_de(p) -> PerguntaOut:
    """`perguntas_store.PerguntaExemplo` → contrato HTTP."""
    return PerguntaOut(
        id=p.id,
        grupo=p.grupo,
        pergunta=p.pergunta,
        pergunta_hash=p.pergunta_hash,
        assunto=p.assunto,
        origem_esperada=p.origem_esperada,
        origem_tambem_ok=list(p.origem_tambem_ok),
        criterio=p.criterio,
        ativo=p.ativo,
    )


def ask_response_de(resultado: Answer, request_id: str) -> AskResponse:
    return AskResponse(
        resposta=resultado.text,
        fontes=[_fonte_para_source_out(c) for c in resultado.sources],
        encontrou_na_base=resultado.grounded,
        origem=resultado.origem,
        cache_hit=resultado.cached,
        request_id=request_id,
    )
