"""Registry de loaders: extensão de arquivo -> BaseLoader do LangChain.

PONTO DE EXTENSÃO — toda fonte nova de conhecimento entra aqui, e só aqui:

  * Web scraping do site da PUC:
        from langchain_community.document_loaders import WebBaseLoader
        register("http", lambda uri: WebBaseLoader(uri))
  * APIs públicas: escreva um BaseLoader que devolva Documents e registre.

O pipeline (chunk -> embedding -> pgvector) não muda em nenhum dos casos, porque
todo loader entrega o mesmo `Document`.
"""

from collections.abc import Callable
from pathlib import Path

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_core.document_loaders import BaseLoader

from app.ingestion.loaders.xlsx_modelos_resposta import ModelosRespostaXlsxLoader

LoaderFactory = Callable[[str], BaseLoader]

_REGISTRY: dict[str, LoaderFactory] = {}


def register(key: str, factory: LoaderFactory) -> None:
    """Associa uma chave (extensão como '.pdf', ou um esquema como 'http') a um loader."""
    _REGISTRY[key.lower()] = factory


def supported_extensions() -> set[str]:
    return {k for k in _REGISTRY if k.startswith(".")}


def loader_for(path: Path) -> BaseLoader | None:
    factory = _REGISTRY.get(path.suffix.lower())
    return factory(str(path)) if factory else None


register(".pdf", lambda uri: PyPDFLoader(uri))
register(".txt", lambda uri: TextLoader(uri, encoding="utf-8"))
register(".md", lambda uri: TextLoader(uri, encoding="utf-8"))
register(".docx", lambda uri: Docx2txtLoader(uri))
# Forma específica de `.xlsx`: modelos de e-mail já pré-chunkados pelo
# notebook `ler_dados_pst` (ver `xlsx_modelos_resposta.py`). Não é loader
# genérico de planilha — se um dia existir uma segunda forma de `.xlsx` na
# ingestão, o registro por extensão sozinho deixa de bastar e o despacho
# precisa olhar o conteúdo, não só o sufixo.
register(".xlsx", lambda uri: ModelosRespostaXlsxLoader(uri))
