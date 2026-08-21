"""Fábricas dos modelos de IA.

Único lugar do projeto que conhece o provedor de IA. Trocar de provedor =
trocar este arquivo, desde que as classes devolvidas continuem implementando
`Embeddings` e `BaseChatModel` do LangChain.

Embeddings rodam localmente (HuggingFace/sentence-transformers) para não
depender de cota de API — o chat continua no Gemini, que é usado bem menos
(uma chamada por pergunta do usuário, contra uma por chunk na ingestão).
"""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_huggingface import HuggingFaceEmbeddings

from app.core.config import settings


def _require_api_key() -> str:
    if not settings.google_api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY não configurada. Copie .env.example para .env e preencha."
        )
    return settings.google_api_key


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


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    """T2.5 — `timeout` e `max_retries` explícitos.

    As rotas da API são síncronas (`def`), então o FastAPI as roda no
    threadpool: uma chamada pendurada no Gemini segura uma thread até o socket
    morrer sozinho. Poucas dessas e o pool acaba — o servidor inteiro para de
    responder, inclusive as perguntas que sairiam do cache sem tocar no LLM.

    `max_retries` também é explícito porque o default da lib é 6, e timeout sem
    limitar retry não resolve nada: o pior caso vira `6 * timeout`. Com 2 e 30s,
    o teto de espera é o dobro do orçamento, não doze vezes.
    """
    return ChatGoogleGenerativeAI(
        model=settings.chat_model,
        google_api_key=_require_api_key(),
        temperature=0.1,  # suporte acadêmico: previsibilidade > criatividade
        timeout=settings.gemini_timeout,
        max_retries=settings.gemini_max_retries,
    )
