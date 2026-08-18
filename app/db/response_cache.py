"""Cache de respostas, na mesma base Postgres da ingestão.

Tabela própria (não o schema interno do langchain-postgres) porque o que se
guarda aqui não é um embedding, é o texto já gerado pelo LLM para uma chave
que representa "este conjunto de chunks recuperados" (ver `_cache_key` em
`app/agent/responder.py`). Fica isolado aqui pelo mesmo motivo de
`delete_by_source`/`existing_content_hashes`: se o acesso a essa tabela mudar,
só este módulo é afetado.
"""

from functools import lru_cache

from langchain_postgres import PGVector
from sqlalchemy import text

from app.db.vector_store import get_vector_store


@lru_cache(maxsize=1)
def _ensure_table(store: PGVector) -> None:
    stmt = text(
        """
        CREATE TABLE IF NOT EXISTS resposta_cache (
            cache_key TEXT PRIMARY KEY,
            assunto TEXT,
            resposta TEXT NOT NULL,
            criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    with store.session_maker() as session:
        session.execute(stmt)
        session.commit()


def get_cached_answer(cache_key: str, store: PGVector | None = None) -> str | None:
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text("SELECT resposta FROM resposta_cache WHERE cache_key = :cache_key")
    with store.session_maker() as session:
        row = session.execute(stmt, {"cache_key": cache_key}).first()
        return row.resposta if row else None


def set_cached_answer(
    cache_key: str, assunto: str | None, resposta: str, store: PGVector | None = None
) -> None:
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text(
        """
        INSERT INTO resposta_cache (cache_key, assunto, resposta)
        VALUES (:cache_key, :assunto, :resposta)
        ON CONFLICT (cache_key)
        DO UPDATE SET resposta = EXCLUDED.resposta, atualizado_em = now()
        """
    )
    with store.session_maker() as session:
        session.execute(stmt, {"cache_key": cache_key, "assunto": assunto, "resposta": resposta})
        session.commit()
