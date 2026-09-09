"""Pipeline de ingestão: carrega -> enriquece metadata -> chunka -> indexa."""

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

from app.core.config import DATA_RAW_DIR
from app.db.pre_retrieval_cache import clear_pre_retrieval_cache
from app.db.vector_store import (
    delete_by_source,
    existing_content_hashes,
    get_vector_store,
    list_ingested_sources,
)
from app.ingestion.chunker import chunk_id, split_documents
from app.ingestion.loaders.registry import loader_for, supported_extensions

logger = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    arquivos: int = 0
    chunks: int = 0
    duplicados: int = 0
    pulados: int = 0
    ignorados: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.ignorados = self.ignorados or []


def _enrich(documents: list[Document], path: Path, assunto: str) -> list[Document]:
    """Metadata gravada desde já, mesmo o que a v1 ainda não consulta.

    `source_type` e `source_uri` deixam o índice pronto para conviver com conteúdo
    de scraping/API mais tarde (ex.: priorizar o mais recente) sem migração.
    """
    for doc in documents:
        doc.metadata.update(
            {
                "assunto": assunto,
                "source_type": "file",
                "source_uri": path.as_uri(),
                "source_path": str(path),
                "source_name": path.name,
            }
        )
    return documents


def _indexar_chunks(chunks: list[Document], source_path: str) -> tuple[int, int]:
    """Substitui os chunks de `source_path` pelos novos, pulando os que já existem
    com o mesmo texto em outra fonte.

    Núcleo comum de `ingest_file` (fonte = arquivo em data/raw) e
    `ingest_documents` (fonte = URL crawlada da allowlist, ver scripts/crawl.py).
    O `delete_by_source` antes torna a operação idempotente inclusive quando a
    fonte encolhe; o filtro por `content_hash` evita que o mesmo texto vindo de
    duas fontes (um aviso repetido em vários PDFs, ou a mesma nota numa página e
    num PDF) vire duas linhas quase idênticas competindo pelo `top_k`.
    """
    store = get_vector_store()
    delete_by_source(store, source_path)

    hashes = [c.metadata["content_hash"] for c in chunks]
    duplicados = existing_content_hashes(store, hashes)
    novos = [c for c in chunks if c.metadata["content_hash"] not in duplicados]

    if novos:
        store.add_documents(novos, ids=[chunk_id(c) for c in novos])

    # Invalida o cache PRÉ-RETRIEVAL: a chave dele (`pergunta + assunto`) não tem
    # os ids dos chunks, então nada o invalida sozinho quando a base muda (o
    # cache pós-retrieval consegue isso de graça pela troca de id). Esta é a
    # invalidação — o choke point único de escrita no índice. Limpeza total, não
    # por `source_path`: uma resposta pode ter vindo de qualquer combinação de
    # documentos. Ver `app/db/pre_retrieval_cache.py`.
    clear_pre_retrieval_cache(store)

    return len(novos), len(chunks) - len(novos)


def ingest_file(path: Path, assunto: str) -> tuple[int, int]:
    """Indexa um arquivo. Reingerir o mesmo arquivo substitui o conteúdo anterior.

    Devolve (chunks indexados, chunks descartados por já existir com o mesmo
    conteúdo em outra fonte).
    """
    loader = loader_for(path)
    if loader is None:
        raise ValueError(f"Sem loader para {path.suffix}")

    documents = _enrich(loader.load(), path, assunto)
    chunks = split_documents(documents)
    if not chunks:
        return 0, 0
    return _indexar_chunks(chunks, str(path))


def ingest_documents(documents: list[Document], source_path: str) -> tuple[int, int]:
    """Indexa `Document`s já montados — que NÃO vêm de um arquivo em data/raw.

    Usado pelo crawler da allowlist web (`scripts/crawl.py`): a metadata de
    origem (`source_path`, `source_type="web"`, `assunto`, `source_uri`, ...) é
    responsabilidade de quem chama; aqui só chunka e indexa. `source_path` tem
    que ser estável entre execuções (a URL da página) para o re-crawl semanal
    substituir aquela página em vez de duplicá-la.
    """
    chunks = split_documents(documents)
    if not chunks:
        return 0, 0
    return _indexar_chunks(chunks, source_path)


def ingest_assunto(assunto: str, apenas_novos: bool = False) -> IngestionReport:
    """Indexa os arquivos suportados de data/raw/<assunto>/.

    `apenas_novos=True` pula qualquer arquivo cujo `source_path` já apareça no
    índice, sem reprocessar nem chamar o embedder — poupa a cota/tempo de quem
    só quer indexar o que foi adicionado desde a última rodada. Um arquivo que
    já existe mas mudou de conteúdo não é pego por esse modo (ele só olha o
    caminho, não o hash); para reindexar conteúdo alterado, rode sem a flag.
    """
    pasta = DATA_RAW_DIR / assunto
    if not pasta.is_dir():
        raise FileNotFoundError(f"Pasta não encontrada: {pasta}")

    report = IngestionReport()
    extensoes = supported_extensions()

    ja_indexados: set[str] = set()
    if apenas_novos:
        store = get_vector_store()
        ja_indexados = {source_path for source_path, _ in list_ingested_sources(store)}

    for path in sorted(pasta.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensoes:
            report.ignorados.append(path.name)
            continue
        if apenas_novos and str(path) in ja_indexados:
            report.pulados += 1
            logger.info("%s -> pulado (já indexado)", path.name)
            continue

        n, duplicados = ingest_file(path, assunto)
        report.arquivos += 1
        report.chunks += n
        report.duplicados += duplicados
        if duplicados:
            logger.info("%s -> %d chunk(s), %d duplicado(s) descartado(s)", path.name, n, duplicados)
        else:
            logger.info("%s -> %d chunks", path.name, n)

    return report
