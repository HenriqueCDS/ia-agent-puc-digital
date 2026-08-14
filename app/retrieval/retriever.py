"""Busca dos chunks mais relevantes no pgvector."""

from langchain_postgres import PGVector

from app.core.config import settings
from app.core.models import Query, RetrievedChunk
from app.db.vector_store import get_vector_store


def retrieve(query: Query, store: PGVector | None = None) -> list[RetrievedChunk]:
    """Devolve os chunks acima do limiar de relevância, do mais relevante ao menos.

    PONTO DE EXTENSÃO — reranking e busca híbrida (vetorial + texto) entram aqui,
    entre a busca e o corte por limiar, sem tocar no agente.
    """
    store = store or get_vector_store()

    # O filtro por assunto usa a metadata gravada na ingestão.
    filtro = {"assunto": {"$eq": query.assunto}} if query.assunto else None

    resultados = store.similarity_search_with_relevance_scores(
        query.text,
        k=settings.top_k,
        filter=filtro,
    )

    return [
        RetrievedChunk(document=doc, score=score)
        for doc, score in resultados
        if score >= settings.relevance_threshold
    ]
