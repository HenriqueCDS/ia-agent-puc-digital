"""Testes do agente com retriever e LLM falsos."""

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.agent import responder
from app.agent.prompts import SEM_CONTEXTO
from app.core.config import settings
from app.core.models import Query, RetrievedChunk


class FakeLLM:
    """Dublê de BaseChatModel: guarda o prompt recebido."""

    def __init__(self):
        self.mensagens = None

    def invoke(self, mensagens):
        self.mensagens = mensagens
        return AIMessage(content="Resposta gerada.")


def _chunk(texto="Para enviar a atividade, acesse Tarefas.", **meta):
    return RetrievedChunk(
        document=Document(page_content=texto, metadata={"source_name": "guia.pdf", **meta}),
        score=0.9,
    )


def test_sem_chunks_nao_chama_o_llm(monkeypatch):
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    llm = FakeLLM()

    resultado = responder.answer(Query(text="pergunta fora da base"), llm=llm)

    assert resultado.grounded is False
    assert resultado.text == SEM_CONTEXTO
    assert llm.mensagens is None  # guardrail evitou a chamada


def test_contexto_recuperado_entra_no_prompt(monkeypatch):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(page=1)])
    llm = FakeLLM()

    resultado = responder.answer(Query(text="como envio atividade?"), llm=llm)

    prompt = "\n".join(str(m.content) for m in llm.mensagens)
    assert "acesse Tarefas" in prompt
    assert "guia.pdf, p. 2" in prompt  # a citação vai junto do trecho
    assert "como envio atividade?" in prompt
    assert resultado.grounded is True
    assert resultado.sources


def test_pergunta_vazia_e_rejeitada():
    with pytest.raises(ValueError):
        responder.answer(Query(text="   "), llm=FakeLLM())


def test_anexo_ainda_nao_suportado_falha_explicitamente(tmp_path):
    with pytest.raises(NotImplementedError):
        responder.answer(Query(text="o que é isso?", attachments=[tmp_path / "print.png"]))


def test_2_fontes_fortes_usam_prompt_de_alta_confianca(monkeypatch):
    alto = settings.exact_match_threshold
    chunks = [_chunk(page=1), _chunk(page=2)]
    chunks[0].score = alto
    chunks[1].score = alto
    monkeypatch.setattr(responder, "retrieve", lambda q: chunks)
    llm = FakeLLM()

    responder.answer(Query(text="como envio atividade?"), llm=llm)

    prompt = "\n".join(str(m.content) for m in llm.mensagens)
    assert "sem ressalvas" in prompt


def test_fonte_unica_forte_nao_usa_alta_confianca(monkeypatch):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(page=1)])
    llm = FakeLLM()

    responder.answer(Query(text="como envio atividade?"), llm=llm)

    prompt = "\n".join(str(m.content) for m in llm.mensagens)
    assert "sem ressalvas" not in prompt
