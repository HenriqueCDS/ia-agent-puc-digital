"""Acesso ao pgvector via LangChain."""

import logging

from functools import lru_cache

from langchain_postgres import PGVector
from sqlalchemy import text

from app.core.config import settings
from app.providers.embeddings import get_embeddings

logger = logging.getLogger(__name__)


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


def aquecer() -> None:
    """Carrega o modelo de embeddings e confirma o Postgres AGORA, não na 1ª pergunta.

    `get_embeddings()` (chamado por `get_vector_store()`) lê ~1GB de pesos do
    disco na primeira vez que roda — medido em até 65s neste projeto. Sem este
    pré-aquecimento o custo cai em (a) no primeiro `/v1/ask` depois do deploy ou
    (b) na primeira pergunta de uma rodada de `scripts.eval_run` — onde além da
    espera ainda contamina o `ms_retrieve` daquele item e o tempo de parede da
    rodada. O `SELECT 1` prova a conexão pelo mesmo princípio: falhar visível no
    boot é melhor que falhar na 1ª pergunta.

    Idempotente — `lru_cache` em `get_vector_store`/`get_embeddings`, então
    chamar de novo custa só o `SELECT 1`. É o ponto ÚNICO de warm-up: a API
    (`app/api/app._lifespan`) e a bateria de testes chamam esta função, em vez de
    cada uma repetir a sequência e uma delas esquecer um passo.
    """
    logger.info("warm-up: carregando modelo de embeddings...")
    store = get_vector_store()
    with store.session_maker() as session:
        session.execute(text("SELECT 1"))
    logger.info("warm-up: concluído, conexão com o Postgres confirmada.")


# Subquery reaproveitada por TODAS as funções abaixo: escopa toda leitura/escrita
# à coleção ATIVA (`settings.collection_name`). Sem isso, uma coleção antiga e
# abandonada (ex.: de antes de trocar o provedor de embeddings — ver o comentário
# sobre o sufixo do modelo em `core/config.Settings.collection_name`) apareceria
# misturada em `list_ingested_sources`, seria apagada junto por engano num
# `delete_by_assunto` e bloquearia a ingestão inteira em `existing_content_hashes`,
# mesmo nunca tendo sido usada em nenhuma busca.
#
# "TODAS" é literal e é invariante do módulo, não coincidência: `existing_content_hashes`
# nasceu sem o filtro e o efeito só apareceu meses depois, como uma reingestão que
# indexava zero chunks sem erro nenhum. `tests/test_vector_store.py` trava isso.
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
    """Remove os chunks de ARQUIVOS de um assunto (pasta) inteiro.

    Complementa `delete_by_source`: aquela apaga um arquivo por vez (uso interno
    da reingestão), esta apaga uma pasta inteira de uma vez — o caso de uso da
    CLI de limpeza (`scripts/remove_ingested.py`), quando um assunto sai do
    escopo do agente.

    NÃO toca no conteúdo crawlado (`source_type='web'`): as páginas da
    `WEB_ALLOWLIST` são gravadas com o `assunto` da `FonteWeb`
    (`puc-digital`/`canvas`, não `"web"`, para o retrieval filtrado enxergá-las —
    ver `scripts/crawl.py`), então sem esta cláusula um `--assunto puc-digital`
    para limpar 3 PDFs levava junto todas as páginas web daquele assunto, em
    silêncio (a CLI só pré-visualiza os arquivos, não as URLs). Para apagar o
    crawl use `remove_ingested` com um trecho da URL como termo, ou
    `delete_by_source` por URL. `IS DISTINCT FROM` mantém os chunks antigos sem a
    chave `source_type` (NULL) no resultado.
    """
    stmt = text(
        f"""
        DELETE FROM langchain_pg_embedding
        WHERE cmetadata->>'assunto' = :assunto
          AND cmetadata->>'source_type' IS DISTINCT FROM 'web'
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
    """Entre os hashes dados, devolve os que já existem em QUALQUER fonte da coleção ATIVA.

    Usado antes de indexar para não duplicar chunks cujo texto já foi indexado a
    partir de outro arquivo (ex.: o mesmo aviso colado em dois PDFs).

    "Da coleção ativa" é o ponto, e o filtro por `collection_id` já custou uma
    ingestão inteira em silêncio. Sem ele, esta era a única das cinco funções do
    módulo que enxergava a tabela toda — e `content_hash` é do TEXTO normalizado
    (ver `ingestion/chunker.content_hash`), não do vetor, então ele é idêntico
    entre coleções geradas por modelos de embedding diferentes. O efeito era:
    criar uma coleção nova (trocar de modelo, testar outro chunking) e ver todo
    chunk ser descartado como "duplicado" contra a coleção ANTIGA, deixando a
    nova permanentemente vazia. Zero chunks indexados, zero erros, exit 0.
    """
    if not hashes:
        return set()

    stmt = text(
        f"""
        SELECT DISTINCT cmetadata->>'content_hash' AS content_hash
        FROM langchain_pg_embedding
        WHERE cmetadata->>'content_hash' = ANY(:hashes)
          AND collection_id = {_COLLECTION_ID}
        """
    )
    with store.session_maker() as session:
        rows = session.execute(
            stmt, {"hashes": hashes, "collection_name": store.collection_name}
        )
        return {row.content_hash for row in rows}
