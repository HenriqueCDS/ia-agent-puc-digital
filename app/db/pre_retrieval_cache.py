"""Cache PRÉ-RETRIEVAL: resposta da base indexada por (pergunta normalizada +
assunto), sem os ids dos chunks.

Irmão de `response_cache.py`, e a diferença está toda na CHAVE:

- `response_cache` (pós-retrieval) indexa por `pergunta + assunto + ids dos
  chunks recuperados`. Um hit poupa a chamada ao LLM, mas o retrieval — busca no
  pgvector + rerank cross-encoder — já rodou (é ele que produz os ids da chave).
  Reingerir um documento alterado troca os ids e invalida a entrada sozinho.

- aqui (pré-retrieval) indexa só por `pergunta normalizada + assunto`. Um hit é
  checado ANTES do retrieval em `responder._responder`, então poupa o pipeline
  inteiro: nem pgvector, nem cross-encoder, nem LLM. O preço é que a chave não
  tem como saber que a base mudou — a invalidação é EXPLÍCITA:
  `ingestion.pipeline._indexar_chunks` chama `clear_pre_retrieval_cache()` a cada
  reingestão (o choke point único de escrita no índice). Sem ids na chave, a
  reingestão é o único sinal de "a base mudou".

Só o caminho `origem="base"` bem-sucedido é gravado aqui (ver
`responder._tentar_base`) — veto de contexto, web e encaminhamento não entram.
Desligado no canal `eval` (a suíte precisa medir retrieval + rerank) e quando há
`query.modelo` (a chave não carrega o modelo — mesma razão do cache pós-retrieval).

Isolado neste módulo pelo mesmo motivo de `response_cache.py` e
`telemetry_store.py`: se o acesso à tabela mudar, só aqui é afetado.
"""

import json

from langchain_postgres import PGVector
from sqlalchemy import text

from app.db.vector_store import get_vector_store

# Tabela própria, separada da `resposta_cache`: o `fontes` JSONB e a semântica de
# invalidação (limpeza total na reingestão, sem TTL) são só deste cache, e uma
# tabela à parte deixa `clear_pre_retrieval_cache` e a limpeza da ingestão
# atuarem sem tocar no cache pós-retrieval. `current_schema()` para não casar uma
# homônima de outro schema (a nossa é criada sem qualificação).
_TABELA_PRONTA = text(
    """
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = current_schema()
      AND table_name = 'resposta_cache_pergunta'
    """
)

_CRIAR = text(
    """
    CREATE TABLE IF NOT EXISTS resposta_cache_pergunta (
        cache_key TEXT PRIMARY KEY,
        pergunta_norm TEXT NOT NULL,
        assunto TEXT,
        resposta TEXT NOT NULL,
        fontes JSONB NOT NULL DEFAULT '[]'::jsonb,
        modelo TEXT,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """
)


def _ensure_table(store: PGVector) -> None:
    """Cria a tabela se faltar. Sem `@lru_cache`, pelo mesmo motivo de
    `response_cache._ensure_table` (INF-11): uma tabela dropada em runtime (teste
    que zera a base, manutenção) precisa ser recriada no acesso seguinte, não
    ficar quebrada até o restart. A consulta ao catálogo custa ~µs; em produção a
    tabela é permanente e o ramo do DDL nem é alcançado."""
    with store.session_maker() as session:
        if session.execute(_TABELA_PRONTA).first():
            return
        session.execute(_CRIAR)
        session.commit()


def get_cached_pre_retrieval(
    cache_key: str, store: PGVector | None = None
) -> tuple[str, list[dict]] | None:
    """`(resposta_com_marcador, fontes)` ou `None`.

    `resposta` é o texto CRU, com o marcador de tópico ainda embutido — igual ao
    `response_cache`: quem lê roda `separar_topico` de novo e recupera o tópico
    sem coluna extra. `fontes` é a lista de dicts que `responder` serializou de
    cada `RetrievedChunk` (ver `_fonte_para_json` lá).
    """
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text(
        "SELECT resposta, fontes FROM resposta_cache_pergunta WHERE cache_key = :cache_key"
    )
    with store.session_maker() as session:
        row = session.execute(stmt, {"cache_key": cache_key}).first()
        if row is None:
            return None
        fontes = row.fontes if isinstance(row.fontes, list) else json.loads(row.fontes)
        return row.resposta, fontes


def set_cached_pre_retrieval(
    cache_key: str,
    pergunta_norm: str,
    assunto: str | None,
    resposta: str,
    fontes: list[dict],
    modelo: str | None = None,
    store: PGVector | None = None,
) -> None:
    """Grava (ou atualiza) a resposta da base para esta pergunta.

    `pergunta_norm` e `modelo` são só metadado de auditoria — a chave já é o
    hash de `pergunta_norm + assunto`, e `modelo` (qual provider gerou) não entra
    na busca, igual ao `response_cache`.
    """
    store = store or get_vector_store()
    _ensure_table(store)

    stmt = text(
        """
        INSERT INTO resposta_cache_pergunta
            (cache_key, pergunta_norm, assunto, resposta, fontes, modelo)
        VALUES (:cache_key, :pergunta_norm, :assunto, :resposta, CAST(:fontes AS jsonb), :modelo)
        ON CONFLICT (cache_key) DO UPDATE SET
            resposta = EXCLUDED.resposta,
            fontes = EXCLUDED.fontes,
            modelo = EXCLUDED.modelo,
            atualizado_em = now()
        """
    )
    with store.session_maker() as session:
        session.execute(
            stmt,
            {
                "cache_key": cache_key,
                "pergunta_norm": pergunta_norm,
                "assunto": assunto,
                "resposta": resposta,
                "fontes": json.dumps(fontes, ensure_ascii=False),
                "modelo": modelo,
            },
        )
        session.commit()


def clear_pre_retrieval_cache(store: PGVector | None = None) -> int:
    """Apaga todo o cache pré-retrieval. Chamado a cada reingestão
    (`ingestion.pipeline._indexar_chunks`) — é a invalidação da feature — e pela
    CLI de limpeza (`scripts/clear_cache.py`)."""
    store = store or get_vector_store()
    _ensure_table(store)

    with store.session_maker() as session:
        result = session.execute(text("DELETE FROM resposta_cache_pergunta"))
        session.commit()
        return result.rowcount or 0
