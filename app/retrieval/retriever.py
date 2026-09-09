"""Busca dos chunks mais relevantes no pgvector."""

import logging

from langchain_postgres import PGVector

from app.core import telemetry
from app.core.config import settings
from app.core.models import Query, RetrievedChunk
from app.db.vector_store import get_vector_store

logger = logging.getLogger(__name__)


def retrieve(query: Query, store: PGVector | None = None) -> list[RetrievedChunk]:
    """Devolve os chunks acima do limiar de relevância, do mais relevante ao menos.

    Dois caminhos, decididos por `settings.reranker_enabled`:

    - **desligado (padrão)**: bi-encoder puro. Busca `TOP_K` candidatos no E5 e
      corta por `RELEVANCE_THRESHOLD`. É o comportamento histórico.
    - **ligado**: 2 estágios (RET-3). O E5 traz `RERANKER_CANDIDATES` (recall
      amplo), o cross-encoder reordena esses candidatos e reescreve `.score` na
      escala dele, e o corte final passa a ser por `RERANKER_THRESHOLD` — MAS o
      piso de `RELEVANCE_THRESHOLD` continua valendo no 1º estágio (RET-7, ver
      abaixo).

    Em ambos, devolve `list[RetrievedChunk]` ordenada e no máximo `TOP_K` itens —
    o agente (`responder.py`), o guardrail e a borda HTTP não enxergam a
    diferença, só os scores mudam de escala quando o reranker está ligado.

    Quando o 2º estágio roda, sua latência vai para `telemetry.ms_rerank`
    (subconjunto de `ms_retrieve`) — é a métrica que o A/B do RET-3 precisa
    (INF-9). Fora de `answer()` a medição é um no-op.

    **RET-8** — o cross-encoder é uma dependência que pode falhar em runtime
    (estoura a RAM da VM junto do E5, `sentence_transformers` ausente, modelo
    corrompido). Se `rerank` levanta, o retrieval NÃO propaga o erro: cai para
    a ordem do bi-encoder e o corte de `relevance_threshold`, como se
    `RERANKER_ENABLED=false` — perfil §7, mesmo espírito da `ProviderChain`. A
    telemetria distingue os três estados por `reranker_aplicado`: `None` (2º
    estágio desligado), `True` (rodou), `False` (ligado, mas falhou e caiu) —
    ver `responder._responder`.
    """
    store = store or get_vector_store()

    # O filtro por assunto usa a metadata gravada na ingestão.
    filtro = {"assunto": {"$eq": query.assunto}} if query.assunto else None
    piso_e5 = settings.relevance_threshold

    if not settings.reranker_enabled:
        resultados = store.similarity_search_with_relevance_scores(
            query.text, k=settings.top_k, filter=filtro
        )
        chunks = [RetrievedChunk(document=doc, score=score) for doc, score in resultados]
        return [c for c in chunks if c.score >= piso_e5][: settings.top_k]

    # --- 2 estágios (RET-3) ---
    resultados = store.similarity_search_with_relevance_scores(
        query.text, k=settings.reranker_candidates, filter=filtro
    )
    # RET-7 — o piso de E5 é aplicado ANTES do rerank: lixo fora de domínio nem
    # chega ao cross-encoder, e o resultado final respeita `RELEVANCE_THRESHOLD`
    # independentemente de `RERANKER_THRESHOLD` estar calibrado.
    candidatos = [
        RetrievedChunk(document=doc, score=score)
        for doc, score in resultados
        if score >= piso_e5
    ]
    if not candidatos:
        return []

    try:
        # Import local: com o reranker desligado (o padrão), este módulo — e o
        # boot inteiro — não toca em `sentence_transformers` / torch. Mesmo
        # motivo do import local em `db/vector_store.aquecer`. Dentro do `try`
        # de propósito: um `RERANKER_ENABLED=true` com a lib ausente degrada
        # para o bi-encoder em vez de derrubar o `/ask` (RET-8).
        from app.retrieval.reranker import rerank

        # INF-9 — cronometra só o 2º estágio, para o A/B do RET-3 medir o custo
        # do cross-encoder isolado. Fora de `answer()` (teste de unidade daqui)
        # `telemetry.etapa` é no-op.
        try:
            with telemetry.etapa("ms_rerank"):
                chunks = rerank(query.text, chunks)
            limiar = settings.reranker_threshold
        except Exception:
            # RET-8 — falha do cross-encoder não derruba o `/ask`. Cai para a
            # ordem do bi-encoder (E5 já devolve os candidatos ordenados) e o
            # corte de `relevance_threshold`, o mesmo do caminho desligado.
            # `chunks` segue com a lista do 1º estágio: a atribuição acima só
            # acontece se `rerank` retorna. `score_bruto` fica `None`, o que
            # `responder` lê como `reranker_aplicado=False`.
            logger.warning(
                "rerank falhou; retrieval caiu para a ordem bi-encoder", exc_info=True
            )
            limiar = settings.relevance_threshold
    else:
        limiar = settings.relevance_threshold

    return [c for c in chunks if c.score >= limiar][: settings.top_k]
