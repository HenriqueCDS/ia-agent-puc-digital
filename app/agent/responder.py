"""Orquestração da resposta: pergunta -> retrieval -> prompt -> LLM."""

import hashlib
import logging

from langchain_core.language_models import BaseChatModel

from app.agent.preprocess import normalize
from app.agent.prompts import ANSWER_PROMPT, ANSWER_PROMPT_ALTA_CONFIANCA, SEM_CONTEXTO
from app.core.config import settings
from app.core.models import Answer, Query, RetrievedChunk
from app.db.response_cache import get_cached_answer, set_cached_answer
from app.providers.gemini import get_chat_model
from app.retrieval.retriever import is_exact_match, retrieve

logger = logging.getLogger(__name__)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n---\n\n".join(
        f"[Fonte: {c.citation}]\n{c.document.page_content}" for c in chunks
    )


def _cache_key(query: Query, chunks: list[RetrievedChunk], alta_confianca: bool) -> str:
    """Chave pelo conjunto de chunks recuperados, não pelo texto da pergunta.

    `document.id` é o id determinístico gravado na ingestão (`chunk_id` em
    ingestion/chunker.py) — o PGVector devolve esse mesmo id em cada busca.
    Perguntas parafraseadas que recuperam o mesmo topo caem na mesma chave, e
    reingerir um arquivo alterado muda os ids recuperados e invalida a chave
    sozinho, sem precisar de nenhuma limpeza manual de cache.
    """
    ids = sorted(c.document.id or "" for c in chunks)
    base = f"{query.assunto or ''}|{alta_confianca}|{','.join(ids)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def answer(query: Query, llm: BaseChatModel | None = None) -> Answer:
    """Responde à pergunta usando apenas o que foi recuperado da base.

    Os passos ficam explícitos (em vez de uma chain fechada) porque é aqui que a
    qualidade do RAG é depurada: dá para inspecionar o contexto recuperado antes
    da chamada ao LLM.

    PONTO DE EXTENSÃO — quando entrarem APIs públicas consultadas em tempo real,
    elas viram mais uma fonte de contexto concatenada aqui, ou o agente passa a
    usar tool calling tendo `retrieve` como uma das tools.
    """
    query = normalize(query)
    chunks = retrieve(query)

    # Guardrail: em suporte acadêmico, não responder é melhor que alucinar um
    # procedimento. Também sinaliza quais documentos faltam na base.
    if not chunks:
        return Answer(text=SEM_CONTEXTO, sources=[], grounded=False)

    alta_confianca = is_exact_match(chunks)
    if alta_confianca:
        logger.info("alta confiança: %.2f e %.2f nas 2 fontes do topo", chunks[0].score, chunks[1].score)
    prompt = ANSWER_PROMPT_ALTA_CONFIANCA if alta_confianca else ANSWER_PROMPT

    # Cache pelo conjunto de chunks (ver _cache_key), checado só agora — depois
    # do guardrail acima — para não mascarar uma pergunta sem contexto e não
    # exigir GOOGLE_API_KEY configurada quando a resposta já está cacheada.
    cache_key = _cache_key(query, chunks, alta_confianca) if settings.cache_enabled else None
    if cache_key:
        cache_hit = get_cached_answer(cache_key)
        if cache_hit is not None:
            logger.info("cache hit (%s...)", cache_key[:8])
            return Answer(text=cache_hit, sources=chunks, grounded=True)

    llm = llm or get_chat_model()
    mensagens = prompt.format_messages(
        contexto=_format_context(chunks),
        pergunta=query.text,
    )
    resposta = llm.invoke(mensagens)
    texto = str(resposta.content).strip()

    if cache_key:
        set_cached_answer(cache_key, query.assunto, texto)

    return Answer(text=texto, sources=chunks, grounded=True)
