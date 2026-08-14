"""Acesso ao pgvector via LangChain."""

from functools import lru_cache

from langchain_postgres import PGVector
from sqlalchemy import text

from app.core.config import settings
from app.providers.gemini import get_embeddings


@lru_cache(maxsize=1)
def get_vector_store() -> PGVector:
    """Conecta (e cria, se preciso) a coleção de chunks.

    `embedding_length` fica em None de propósito: a coluna é criada como `vector`
    sem dimensão fixa, então trocar de modelo de embedding não exige migração.
    O preço é não poder criar índice HNSW ainda — o que só passa a importar acima
    de ~50k chunks, onde a busca exata (seq scan) deixa de ser rápida.
    """
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=settings.collection_name,
        connection=settings.database_url,
        use_jsonb=True,
    )


def delete_by_source(store: PGVector, source_path: str) -> int:
    """Remove os chunks de um arquivo antes de reindexá-lo.

    Torna a ingestão idempotente inclusive quando o arquivo encolhe (upsert por id
    sozinho deixaria chunks órfãos do conteúdo antigo).

    Acopla ao schema interno do langchain-postgres (`langchain_pg_embedding`),
    por isso está isolado aqui: se a integração mudar o schema, só este ponto quebra.
    """
    stmt = text(
        """
        DELETE FROM langchain_pg_embedding
        WHERE cmetadata->>'source_path' = :source_path
        """
    )
    with store.session_maker() as session:
        result = session.execute(stmt, {"source_path": source_path})
        session.commit()
        return result.rowcount or 0
