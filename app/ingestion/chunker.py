"""Divisão dos documentos em chunks.

Função pura (sem banco, sem rede) de propósito: é o componente que mais vai ser
ajustado por tentativa e erro, e assim dá para testá-lo sozinho.
"""

import hashlib

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings


def split_documents(documents: list[Document]) -> list[Document]:
    """Quebra os documentos em chunks com overlap, preservando a metadata de origem."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(documents)

    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = i
    return [c for c in chunks if c.page_content.strip()]


def chunk_id(chunk: Document) -> str:
    """Id determinístico: reingerir o mesmo conteúdo atualiza a linha em vez de duplicar."""
    base = f"{chunk.metadata.get('source_path', '')}|{chunk.metadata.get('chunk_index', 0)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()
