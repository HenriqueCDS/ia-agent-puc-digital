"""Fábricas dos modelos Gemini.

Único lugar do projeto que conhece o provedor de IA. Trocar Gemini por outro
provedor = trocar este arquivo, desde que as classes devolvidas continuem
implementando `Embeddings` e `BaseChatModel` do LangChain.
"""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from app.core.config import settings


def _require_api_key() -> str:
    if not settings.google_api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY não configurada. Copie .env.example para .env e preencha."
        )
    return settings.google_api_key


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    return GoogleGenerativeAIEmbeddings(
        model=settings.embedding_model,
        google_api_key=_require_api_key(),
    )


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    return ChatGoogleGenerativeAI(
        model=settings.chat_model,
        google_api_key=_require_api_key(),
        temperature=0.1,  # suporte acadêmico: previsibilidade > criatividade
    )
