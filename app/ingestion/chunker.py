"""Divisão dos documentos em chunks.

Função pura (sem banco, sem rede) de propósito: é o componente que mais vai ser
ajustado por tentativa e erro, e assim dá para testá-lo sozinho.
"""

import hashlib
import logging
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")

# Tamanho do shingle (janela de palavras) para a assinatura de similaridade.
# 4 palavras: curto o bastante para casar dois trechos que só diferem em número
# de página / data / pontuação, longo o bastante para NÃO casar dois parágrafos
# distintos que compartilham um jargão comum ("no ambiente virtual você deve").
_SHINGLE_K = 4


def split_documents(documents: list[Document]) -> list[Document]:
    """Quebra os documentos em chunks com overlap, preservando a metadata de origem."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    chunks = [c for c in chunks if c.page_content.strip()]
    chunks = deduplicar_similares(chunks, settings.ingest_dedup_similaridade)

    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = i
        chunk.metadata["content_hash"] = content_hash(chunk.page_content)
    return chunks


def _shingles(texto: str, k: int = _SHINGLE_K) -> frozenset[str]:
    """Conjunto de janelas de `k` palavras do texto normalizado (caixa/espaço).

    É a representação sobre a qual a similaridade de Jaccard é medida. Texto com
    menos de `k` palavras vira um shingle único (ele mesmo) — dois rodapés de
    3 palavras idênticos ainda casam.
    """
    palavras = _WHITESPACE.sub(" ", texto.strip().lower()).split()
    if len(palavras) < k:
        return frozenset([" ".join(palavras)]) if palavras else frozenset()
    return frozenset(" ".join(palavras[i : i + k]) for i in range(len(palavras) - k + 1))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    intersecao = len(a & b)
    return intersecao / (len(a) + len(b) - intersecao)


def deduplicar_similares(chunks: list[Document], limiar: float) -> list[Document]:
    """Descarta chunks quase idênticos a um já mantido (RET-6).

    O caso real: `Canvas_Student_Guide.pdf` tem 1108 páginas e repete o mesmo
    procedimento ("Submit Assignment") em dezenas delas com texto quase igual —
    o `content_hash` (exato) não pega, e o retrieval devolve 5 chunks
    praticamente iguais, o que zera a margem relativa (RET-2) e faz a resposta
    parecer mais coberta do que é (Q11/Q23).

    Mantém a PRIMEIRA ocorrência (a página mais no começo do documento) e
    compara só contra as mantidas — se o documento é uma repetição só, a lista
    de assinaturas fica pequena e o custo desaba. Pior caso (documento todo
    distinto): O(n²) em Jaccard de shingles, aceitável porque roda uma vez, na
    ingestão, fora do caminho de resposta.

    `limiar <= 0` desliga (o default de `INGEST_DEDUP_SIMILARIDADE` é 0.9). Não
    reindexa `chunk_index` aqui de propósito — quem chama (`split_documents`)
    faz isso depois, sobre a lista já filtrada.
    """
    if not limiar or limiar <= 0 or len(chunks) < 2:
        return chunks

    mantidos: list[Document] = []
    assinaturas: list[frozenset[str]] = []
    removidos = 0
    for chunk in chunks:
        assinatura = _shingles(chunk.page_content)
        if any(_jaccard(assinatura, vista) >= limiar for vista in assinaturas):
            removidos += 1
            continue
        mantidos.append(chunk)
        assinaturas.append(assinatura)

    if removidos:
        logger.info(
            "dedup: %d de %d chunk(s) descartado(s) por similaridade >= %.2f com outro do mesmo lote",
            removidos,
            len(chunks),
            limiar,
        )
    return mantidos


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
