"""Fábrica do modelo de embeddings.

Vive em módulo próprio (e não mais em `gemini.py`) porque não tem nada a ver com
o provedor de chat: os embeddings rodam LOCALMENTE, via HuggingFace/
sentence-transformers, para não depender de cota de API — na ingestão é uma
chamada por chunk, contra uma por pergunta no chat. Enquanto a única fábrica do
arquivo era o Gemini isso passava; com três provedores de chat na cadeia
(`app/providers/chain.py`), manter `get_embeddings` dentro de `gemini.py` seria
só o nome errado no import de quem usa.
"""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings

from app.core.config import settings


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """Modelo local. Tenta primeiro em modo offline (sem bater no HF Hub) —

    depois do primeiro download o modelo já está em cache, então checar a rede
    a cada execução só custa latência. Só volta a acessar a rede (e baixa/
    atualiza o cache) se ainda não tiver nada salvo localmente — nesse caso,
    usa `hf_token` (se configurado) para maior rate limit no download.
    """
    model_kwargs = {"token": settings.hf_token} if settings.hf_token else {}
    try:
        return HuggingFaceEmbeddings(
            model_name=settings.embedding_model,
            model_kwargs={**model_kwargs, "local_files_only": True},
        )
    except OSError:
        return HuggingFaceEmbeddings(
            model_name=settings.embedding_model,
            model_kwargs=model_kwargs,
        )
