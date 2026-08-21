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


# Subquery reaproveitada pelas três funções abaixo: escopa toda leitura/escrita
# à coleção ATIVA (`settings.collection_name`). Sem isso, uma coleção antiga e
# abandonada (ex.: de antes de trocar o provedor de embeddings — ver o comentário
# sobre o sufixo "_hf" em `core/config.Settings.collection_name`) apareceria
# misturada em `list_ingested_sources` e seria apagada junto por engano num
# `delete_by_assunto`, mesmo nunca tendo sido usada em nenhuma busca.
_COLLECTION_ID = (
    "(SELECT uuid FROM langchain_pg_collection WHERE name = :collection_name)"
)


def delete_by_source(store: PGVector, source_path: str) -> int:
    """Remove os chunks de um arquivo antes de reindexá-lo.

    Torna a ingestão idempotente inclusive quando o arquivo encolhe (upsert por id
    sozinho deixaria chunks órfãos do conteúdo antigo).

    Acopla ao schema interno do langchain-postgres (`langchain_pg_embedding`),
    por isso está isolado aqui: se a integração mudar o schema, só este ponto quebra.
    """
    stmt = text(
        f"""
        DELETE FROM langchain_pg_embedding
        WHERE cmetadata->>'source_path' = :source_path
          AND collection_id = {_COLLECTION_ID}
        """
    )
    with store.session_maker() as session:
        result = session.execute(
            stmt, {"source_path": source_path, "collection_name": store.collection_name}
        )
        session.commit()
        return result.rowcount or 0


def delete_by_assunto(store: PGVector, assunto: str) -> int:
    """Remove todos os chunks de um assunto (pasta) inteiro.

    Complementa `delete_by_source`: aquela apaga um arquivo por vez (uso interno
    da reingestão), esta apaga uma pasta inteira de uma vez — o caso de uso da
    CLI de limpeza (`scripts/remove_ingested.py`), quando um assunto sai do
    escopo do agente.
    """
    stmt = text(
        f"""
        DELETE FROM langchain_pg_embedding
        WHERE cmetadata->>'assunto' = :assunto
          AND collection_id = {_COLLECTION_ID}
        """
    )
    with store.session_maker() as session:
        result = session.execute(
            stmt, {"assunto": assunto, "collection_name": store.collection_name}
        )
        session.commit()
        return result.rowcount or 0


def list_ingested_sources(store: PGVector) -> list[tuple[str, int]]:
    """Lista os arquivos indexados (na coleção ativa) e a quantidade de chunks de cada um."""
    stmt = text(
        f"""
        SELECT cmetadata->>'source_path' AS source_path, COUNT(*) AS chunks
        FROM langchain_pg_embedding
        WHERE collection_id = {_COLLECTION_ID}
        GROUP BY 1
        ORDER BY 1
        """
    )
    with store.session_maker() as session:
        rows = session.execute(stmt, {"collection_name": store.collection_name})
        return [(row.source_path, row.chunks) for row in rows]


def list_assuntos(store: PGVector) -> list[str]:
    """Assuntos distintos indexados na coleção ativa.

    Fonte de verdade para `GET /v1/assuntos` e para validar o parâmetro
    `assunto` da API antes de chamar `answer()` (ver app/api/deps.py) — hoje um
    assunto que não existe passa pelo filtro `$eq` do retrieval sem erro, dá
    zero chunks e cai silenciosamente no fallback web.
    """
    stmt = text(
        f"""
        SELECT DISTINCT cmetadata->>'assunto' AS assunto
        FROM langchain_pg_embedding
        WHERE collection_id = {_COLLECTION_ID}
          AND cmetadata->>'assunto' IS NOT NULL
        ORDER BY 1
        """
    )
    with store.session_maker() as session:
        rows = session.execute(stmt, {"collection_name": store.collection_name})
        return [row.assunto for row in rows]


def existing_content_hashes(store: PGVector, hashes: list[str]) -> set[str]:
    """Entre os hashes dados, devolve os que já existem em QUALQUER fonte no índice.

    Usado antes de indexar para não duplicar chunks cujo texto já foi indexado a
    partir de outro arquivo (ex.: o mesmo aviso colado em dois PDFs).
    """
    if not hashes:
        return set()

    stmt = text(
        """
        SELECT DISTINCT cmetadata->>'content_hash' AS content_hash
        FROM langchain_pg_embedding
        WHERE cmetadata->>'content_hash' = ANY(:hashes)
        """
    )
    with store.session_maker() as session:
        rows = session.execute(stmt, {"hashes": hashes})
        return {row.content_hash for row in rows}
