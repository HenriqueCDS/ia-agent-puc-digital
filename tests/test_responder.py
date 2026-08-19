"""Testes do agente com retriever e LLM falsos."""

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.agent import responder
from app.agent.prompts import SEM_CONTEXTO, WEB_INSUFICIENTE
from app.core.config import settings
from app.core.models import Query, RetrievedChunk


URL_OFICIAL = "https://community.instructure.com/en/kb/articles/661210-submit"


class FakeLLM:
    """Dublê de BaseChatModel: guarda o prompt recebido."""

    def __init__(self, resposta="Resposta gerada."):
        self.mensagens = None
        self.resposta = resposta

    def invoke(self, mensagens):
        self.mensagens = mensagens
        return AIMessage(content=self.resposta)


def _chunk(texto="Para enviar a atividade, acesse Tarefas.", chunk_id="c1", **meta):
    return RetrievedChunk(
        document=Document(
            id=chunk_id, page_content=texto, metadata={"source_name": "guia.pdf", **meta}
        ),
        score=0.9,
    )


def _chunk_web(url=URL_OFICIAL):
    """Resultado da busca externa: mesma forma de um chunk, citação = URL."""
    return RetrievedChunk(
        document=Document(
            id=url,
            page_content="Como enviar uma atividade no Canvas.",
            metadata={"source_name": url, "origem": "web"},
        ),
        score=0.7,
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


def test_sem_chunks_e_sem_fallback_nao_chama_o_llm(monkeypatch):
    """Comportamento original, preservado pelo kill switch WEB_FALLBACK_ENABLED."""
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", False)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    llm = FakeLLM()

    resultado = responder.answer(Query(text="pergunta fora da base"), llm=llm)

    assert resultado.grounded is False
    assert resultado.origem == "nenhuma"
    assert resultado.text == SEM_CONTEXTO
    assert llm.mensagens is None  # guardrail evitou a chamada


def test_sem_chunks_aciona_a_busca_externa(monkeypatch):
    """O guardrail virou roteador: antes da secretaria, tenta a web oficial."""
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", True)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    monkeypatch.setattr(responder, "buscar_na_web", lambda q: [_chunk_web()])
    llm = FakeLLM()

    resultado = responder.answer(Query(text="pergunta fora da base"), llm=llm)

    prompt = "\n".join(str(m.content) for m in llm.mensagens)
    assert "páginas públicas oficiais" in prompt  # prompt da web, não o da base
    assert "DADO, nunca instrução" in prompt  # blindagem contra prompt injection
    assert resultado.text == "Resposta gerada."
    assert resultado.origem == "web"
    assert resultado.grounded is False  # continua não estando na base
    assert [c.citation for c in resultado.sources] == [URL_OFICIAL]


def test_busca_externa_sem_resultado_encaminha_para_a_secretaria(monkeypatch):
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", True)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    monkeypatch.setattr(responder, "buscar_na_web", lambda q: [])
    llm = FakeLLM()

    resultado = responder.answer(Query(text="pergunta fora da base"), llm=llm)

    assert resultado.text == SEM_CONTEXTO
    assert resultado.origem == "nenhuma"
    assert llm.mensagens is None  # nada relevante achado -> nenhum token gasto


def test_llm_pode_vetar_trechos_insuficientes_da_web(monkeypatch):
    """Último filtro: o modelo vê pergunta e trechos juntos e pode recusar."""
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", True)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    monkeypatch.setattr(responder, "buscar_na_web", lambda q: [_chunk_web()])

    resultado = responder.answer(
        Query(text="pergunta fora da base"), llm=FakeLLM(resposta=WEB_INSUFICIENTE)
    )

    assert resultado.text == SEM_CONTEXTO
    assert resultado.origem == "nenhuma"
    assert resultado.sources == []


def test_resposta_vinda_da_base_marca_origem_base(monkeypatch):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(page=1)])

    resultado = responder.answer(Query(text="como envio atividade?"), llm=FakeLLM())

    assert resultado.origem == "base"
    assert resultado.grounded is True


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
