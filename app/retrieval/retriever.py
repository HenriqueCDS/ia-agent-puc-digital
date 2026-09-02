"""Busca dos chunks mais relevantes no pgvector."""

from langchain_postgres import PGVector

from app.core import telemetry
from app.core.config import settings
from app.core.models import Query, RetrievedChunk
from app.db.vector_store import get_vector_store


def retrieve(query: Query, store: PGVector | None = None) -> list[RetrievedChunk]:
    """Devolve os chunks acima do limiar de relevância, do mais relevante ao menos.

    Dois caminhos, decididos por `settings.reranker_enabled`:

    - **desligado (padrão)**: bi-encoder puro. Busca `TOP_K` candidatos no E5 e
      corta por `RELEVANCE_THRESHOLD`. É o comportamento histórico.
    - **ligado**: 2 estágios (RET-3). O E5 traz `RERANKER_CANDIDATES` (recall
      amplo), o cross-encoder reordena esses candidatos e reescreve `.score` na
      escala dele, e o corte passa a ser por `RERANKER_THRESHOLD`.

    Em ambos, devolve `list[RetrievedChunk]` ordenada e no máximo `TOP_K` itens —
    o agente (`responder.py`), o guardrail e a borda HTTP não enxergam a
    diferença, só os scores mudam de escala quando o reranker está ligado.

    Quando o 2º estágio roda, sua latência vai para `telemetry.ms_rerank`
    (subconjunto de `ms_retrieve`) — é a métrica que o A/B do RET-3 precisa
    (INF-9). Fora de `answer()` a medição é um no-op.
    """
    store = store or get_vector_store()

    # O filtro por assunto usa a metadata gravada na ingestão.
    filtro = {"assunto": {"$eq": query.assunto}} if query.assunto else None

    k = settings.reranker_candidates if settings.reranker_enabled else settings.top_k
    resultados = store.similarity_search_with_relevance_scores(
        query.text,
        k=k,
        filter=filtro,
    )
    chunks = [RetrievedChunk(document=doc, score=score) for doc, score in resultados]

    if settings.reranker_enabled:
        # Import local: com o reranker desligado (o padrão), este módulo — e o
        # boot inteiro — não toca em `sentence_transformers` / torch. Mesmo
        # motivo do import local em `db/vector_store.aquecer`.
        from app.retrieval.reranker import rerank

        # INF-9 — cronometra só o 2º estágio, para o A/B do RET-3 medir o custo
        # do cross-encoder isolado. Fora de `answer()` (teste de unidade daqui)
        # `telemetry.etapa` é no-op.
        with telemetry.etapa("ms_rerank"):
            chunks = rerank(query.text, chunks)
        limiar = settings.reranker_threshold
    else:
        limiar = settings.relevance_threshold

    return [c for c in chunks if c.score >= limiar][: settings.top_k]
