"""Orquestração da resposta: pergunta -> retrieval -> prompt -> LLM."""

import logging

from langchain_core.language_models import BaseChatModel

from app.agent.preprocess import normalize
from app.agent.prompts import ANSWER_PROMPT, ANSWER_PROMPT_ALTA_CONFIANCA, SEM_CONTEXTO
from app.core.models import Answer, Query, RetrievedChunk
from app.providers.gemini import get_chat_model
from app.retrieval.retriever import is_exact_match, retrieve

logger = logging.getLogger(__name__)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n---\n\n".join(
        f"[Fonte: {c.citation}]\n{c.document.page_content}" for c in chunks
    )


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

    llm = llm or get_chat_model()
    if is_exact_match(chunks):
        logger.info("alta confiança: %.2f e %.2f nas 2 fontes do topo", chunks[0].score, chunks[1].score)
        prompt = ANSWER_PROMPT_ALTA_CONFIANCA
    else:
        prompt = ANSWER_PROMPT
    mensagens = prompt.format_messages(
        contexto=_format_context(chunks),
        pergunta=query.text,
    )
    resposta = llm.invoke(mensagens)

    return Answer(text=str(resposta.content).strip(), sources=chunks, grounded=True)
