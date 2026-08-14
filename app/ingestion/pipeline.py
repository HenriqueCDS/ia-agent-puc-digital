"""Pipeline de ingestão: carrega -> enriquece metadata -> chunka -> indexa."""

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

from app.core.config import DATA_RAW_DIR
from app.db.vector_store import delete_by_source, get_vector_store
from app.ingestion.chunker import chunk_id, split_documents
from app.ingestion.loaders.registry import loader_for, supported_extensions

logger = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    arquivos: int = 0
    chunks: int = 0
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


def ingest_file(path: Path, assunto: str) -> int:
    """Indexa um arquivo. Reingerir o mesmo arquivo substitui o conteúdo anterior."""
    loader = loader_for(path)
    if loader is None:
        raise ValueError(f"Sem loader para {path.suffix}")

    documents = _enrich(loader.load(), path, assunto)
    chunks = split_documents(documents)
    if not chunks:
        return 0

    store = get_vector_store()
    delete_by_source(store, str(path))
    store.add_documents(chunks, ids=[chunk_id(c) for c in chunks])
    return len(chunks)


def ingest_assunto(assunto: str) -> IngestionReport:
    """Indexa todos os arquivos suportados de data/raw/<assunto>/."""
    pasta = DATA_RAW_DIR / assunto
    if not pasta.is_dir():
        raise FileNotFoundError(f"Pasta não encontrada: {pasta}")

    report = IngestionReport()
    extensoes = supported_extensions()

    for path in sorted(pasta.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensoes:
            report.ignorados.append(path.name)
            continue

        n = ingest_file(path, assunto)
        report.arquivos += 1
        report.chunks += n
        logger.info("%s -> %d chunks", path.name, n)

    return report
