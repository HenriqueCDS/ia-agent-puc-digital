"""Cache de respostas, na mesma base Postgres da ingestão.

Tabela própria (não o schema interno do langchain-postgres) porque o que se
guarda aqui não é um embedding, é o texto já gerado pelo LLM para uma chave
que representa "este conjunto de chunks recuperados" (ver `_cache_key` em
`app/agent/responder.py`). Fica isolado aqui pelo mesmo motivo de
`delete_by_source`/`existing_content_hashes`: se o acesso a essa tabela mudar,
só este módulo é afetado.
"""

from langchain_postgres import PGVector
from sqlalchemy import text

from app.db.vector_store import get_vector_store

# A tabela existe E já tem a coluna `modelo` — ou seja, tanto o CREATE quanto a
# migração idempotente abaixo já rodaram. `current_schema()` para não casar uma
# tabela homônima de outro schema (a nossa é criada sem qualificação, então cai
# no schema corrente).
_TABELA_PRONTA = text(
    """
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'resposta_cache'
      AND column_name = 'modelo'
    """
)

_CRIAR = text(
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

# `modelo`: qual provider/modelo gerou a resposta cacheada (ver
# `responder._rotulo_do_modelo`). ADD COLUMN separado do CREATE TABLE de
# propósito — `CREATE TABLE IF NOT EXISTS` é um no-op quando a tabela JÁ existe
# (qualquer instalação anterior a esta coluna), então a migração precisa rodar
# por conta própria. `IF NOT EXISTS` (Postgres 9.6+) a deixa idempotente.
_MIGRAR_MODELO = text("ALTER TABLE resposta_cache ADD COLUMN IF NOT EXISTS modelo TEXT")


def _ensure_table(store: PGVector) -> None:
    """Cria a tabela de cache (e a migração da coluna `modelo`) se faltar.

    Sem `@lru_cache` de propósito (INF-11): cachear o efeito por processo faz o
    código assumir que a tabela, criada uma vez, existe até o fim do processo.
    Uma tabela dropada em runtime (teste que zera a base, manutenção) quebraria
    todo INSERT/SELECT seguinte até o restart, porque o `_ensure_table` nunca
    mais rodaria a DDL. Uma consulta ao catálogo por acesso custa ~µs e some com
    essa classe de bug — em produção a tabela é permanente e o ramo da DDL nem
    é alcançado.

    Checa a COLUNA `modelo`, não só a tabela: é o que garante que a migração
    idempotente também rodou numa base criada por uma versão anterior a ela.
    """
    with store.session_maker() as session:
        if session.execute(_TABELA_PRONTA).first():
            return
        session.execute(_CRIAR)
        session.execute(_MIGRAR_MODELO)
        session.commit()


def get_cached_answer(cache_key: str, store: PGVector | None = None) -> str | None:
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text("SELECT resposta FROM resposta_cache WHERE cache_key = :cache_key")
    with store.session_maker() as session:
        row = session.execute(stmt, {"cache_key": cache_key}).first()
        return row.resposta if row else None


def set_cached_answer(
    cache_key: str,
    assunto: str | None,
    resposta: str,
    modelo: str | None = None,
    store: PGVector | None = None,
) -> None:
    """Grava (ou atualiza) a resposta cacheada.

    `modelo` é só metadado de auditoria — "qual provider/modelo gerou esta
    resposta" (ver `responder._rotulo_do_modelo`) — e NÃO entra na busca nem
    na chave: `_cache_key` já resolve isso (inclui o modelo quando o override
    está em jogo). Aqui ele só fica visível para quem consultar a tabela
    direto, ex.: `SELECT modelo, COUNT(*) FROM resposta_cache GROUP BY modelo`.
    """
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text(
        """
        INSERT INTO resposta_cache (cache_key, assunto, resposta, modelo)
        VALUES (:cache_key, :assunto, :resposta, :modelo)
        ON CONFLICT (cache_key)
        DO UPDATE SET resposta = EXCLUDED.resposta, modelo = EXCLUDED.modelo, atualizado_em = now()
        """
    )
    with store.session_maker() as session:
        session.execute(
            stmt,
            {"cache_key": cache_key, "assunto": assunto, "resposta": resposta, "modelo": modelo},
        )
        session.commit()


def clear_cache(store: PGVector | None = None) -> int:
    """Apaga todo o cache de respostas. Usado pela CLI de limpeza (base zerada p/ testes)."""
    store = store or get_vector_store()
    _ensure_table(store)

    with store.session_maker() as session:
        result = session.execute(text("DELETE FROM resposta_cache"))
        session.commit()
        return result.rowcount or 0
