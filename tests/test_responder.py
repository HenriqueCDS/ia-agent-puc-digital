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


def _chunk(texto="Para enviar a atividade, acesse Tarefas.", chunk_id="c1", **meta):
    return RetrievedChunk(
        document=Document(
            id=chunk_id, page_content=texto, metadata={"source_name": "guia.pdf", **meta}
        ),
        score=0.9,
    )


@pytest.fixture(autouse=True)
def fake_cache(monkeypatch):
    """Cache em memória, isolado por teste — nenhum teste toca o Postgres real."""
    armazenado: dict[str, str] = {}
    monkeypatch.setattr(responder, "get_cached_answer", armazenado.get)
    monkeypatch.setattr(
        responder,
        "set_cached_answer",
        lambda key, assunto, resposta: armazenado.__setitem__(key, resposta),
    )
    return armazenado


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


def test_pergunta_parafraseada_com_mesmos_chunks_usa_cache_e_nao_chama_llm(monkeypatch):
    chunks = [_chunk(page=1)]
    monkeypatch.setattr(responder, "retrieve", lambda q: chunks)

    primeiro = responder.answer(Query(text="como envio a atividade?"), llm=FakeLLM())
    assert primeiro.text == "Resposta gerada."

    llm2 = FakeLLM()
    segundo = responder.answer(Query(text="como faço para mandar a atividade?"), llm=llm2)

    assert llm2.mensagens is None  # mesmo conjunto de chunks -> cache hit, sem chamar o LLM
    assert segundo.text == primeiro.text
    assert segundo.grounded is True
    assert segundo.sources == chunks  # fontes vêm do retrieval atual, não do cache


def test_chunks_diferentes_nao_reusam_a_mesma_entrada_de_cache(monkeypatch):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(chunk_id="c1", page=1)])
    responder.answer(Query(text="pergunta 1"), llm=FakeLLM())

    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(chunk_id="c2", page=2)])
    llm2 = FakeLLM()
    responder.answer(Query(text="pergunta 2"), llm=llm2)

    assert llm2.mensagens is not None  # chunk recuperado é outro -> não reusa cache


def test_cache_desligado_sempre_chama_o_llm(monkeypatch):
    monkeypatch.setattr(responder.settings, "cache_enabled", False)
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(page=1)])

    responder.answer(Query(text="como envio a atividade?"), llm=FakeLLM())

    llm2 = FakeLLM()
    responder.answer(Query(text="como envio a atividade?"), llm=llm2)

    assert llm2.mensagens is not None  # cache_enabled=False -> nunca serve do cache
