"""Divisão dos documentos em chunks.

Função pura (sem banco, sem rede) de propósito: é o componente que mais vai ser
ajustado por tentativa e erro, e assim dá para testá-lo sozinho.
"""

import hashlib
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings

_WHITESPACE = re.compile(r"\s+")


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
        chunk.metadata["content_hash"] = content_hash(chunk.page_content)
    return [c for c in chunks if c.page_content.strip()]


def content_hash(text: str) -> str:
    """Hash do conteúdo normalizado (espaço/caixa), estável entre arquivos diferentes.

    Usado para achar chunks com o mesmo texto vindos de fontes diferentes (ex.: o
    mesmo aviso colado em dois PDFs), o que `chunk_id` não cobre por depender do
    `source_path`.
    """
    normalizado = _WHITESPACE.sub(" ", text).strip().lower()
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()


def chunk_id(chunk: Document) -> str:
    """Id determinístico: reingerir o mesmo conteúdo atualiza a linha em vez de duplicar."""
    base = f"{chunk.metadata.get('source_path', '')}|{chunk.metadata.get('chunk_index', 0)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()
