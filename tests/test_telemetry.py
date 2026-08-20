"""Testes da telemetria por pergunta (ver app/core/telemetry.py).

O que importa aqui não é o valor dos números — é que o registro saia sempre,
com o caminho certo, e sem o texto da pergunta.
"""

import json
import logging

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.agent import responder
from app.core import telemetry
from app.core.models import Query, RetrievedChunk


PERGUNTA = "como envio a atividade no canvas?"


class FakeLLM:
    """Dublê que reporta uso de token, como o ChatGoogleGenerativeAI real."""

    def __init__(self, resposta="Resposta gerada.", uso=None):
        self.resposta = resposta
        self.uso = uso or {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}

    def invoke(self, mensagens):
        return AIMessage(content=self.resposta, usage_metadata=self.uso)


def _chunk(score=0.95, chunk_id="c1"):
    return RetrievedChunk(
        document=Document(
            id=chunk_id, page_content="Acesse Tarefas.", metadata={"source_name": "guia.pdf"}
        ),
        score=score,
    )


class _Registros:
    """Lê as linhas capturadas na hora do assert, não na criação da fixture."""

    def __init__(self, caplog):
        self._caplog = caplog

    def __iter__(self):
        return (
            json.loads(r.message) for r in self._caplog.records if r.name == "telemetria"
        )


@pytest.fixture
def registros(caplog, monkeypatch):
    """Captura as linhas emitidas, já desserializadas.

    `propagate` volta a True porque `configurar_logs()` o desliga em produção
    (para o registro não se misturar aos logs em texto) — e é justamente pela
    propagação que o caplog enxerga.
    """
    monkeypatch.setattr(telemetry.logger, "propagate", True)
    monkeypatch.setattr(responder, "get_cached_answer", lambda key: None)
    monkeypatch.setattr(responder, "set_cached_answer", lambda key, assunto, resposta: None)

    with caplog.at_level(logging.INFO, logger="telemetria"):
        yield _Registros(caplog)


def test_resposta_da_base_registra_custo_qualidade_e_latencia(monkeypatch, registros):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk(), _chunk(chunk_id="c2")])
    telemetry.set_canal("cli")

    responder.answer(Query(text=PERGUNTA, assunto="canvas"), llm=FakeLLM())

    (reg,) = registros
    assert reg["canal"] == "cli"
    assert reg["assunto"] == "canvas"
    assert (reg["origem"], reg["grounded"]) == ("base", True)
    assert (reg["n_chunks"], reg["score_top"]) == (2, 0.95)
    assert reg["alta_confianca"] is True
    assert reg["cache_hit"] is False
    assert (reg["input_tokens"], reg["output_tokens"]) == (120, 30)
    assert reg["ms_retrieve"] is not None and reg["ms_llm"] is not None
    assert reg["ms_web"] is None  # etapa que não aconteceu, e o log diz isso
    assert reg["erro"] is None


def test_o_texto_da_pergunta_nunca_e_registrado(monkeypatch, registros):
    """Requisito de privacidade: só assunto + hash (ver WEB_BLOCKLIST)."""
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk()])
    sensivel = "meu boleto da mensalidade 2026 nao chegou, RA 12345"

    responder.answer(Query(text=sensivel), llm=FakeLLM())

    (reg,) = registros
    linha = json.dumps(reg, ensure_ascii=False)
    assert "boleto" not in linha and "12345" not in linha
    assert reg["pergunta_hash"] == telemetry.hash_pergunta(sensivel)


def test_hash_agrupa_a_mesma_pergunta_e_separa_perguntas_diferentes():
    """É esse agrupamento que revela qual documento falta indexar."""
    assert telemetry.hash_pergunta("  Como Envio? ") == telemetry.hash_pergunta("como envio?")
    assert telemetry.hash_pergunta("como envio?") != telemetry.hash_pergunta("como acesso?")


def test_cache_hit_registra_resposta_sem_token_nem_chamada(monkeypatch, registros):
    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk()])
    monkeypatch.setattr(responder, "get_cached_answer", lambda key: "Resposta cacheada.")

    responder.answer(Query(text=PERGUNTA), llm=FakeLLM())

    (reg,) = registros
    assert reg["cache_hit"] is True
    assert reg["input_tokens"] is None and reg["output_tokens"] is None
    assert reg["ms_llm"] is None


def test_caminho_da_web_registra_origem_e_latencia_da_busca(monkeypatch, registros):
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", True)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    monkeypatch.setattr(
        responder,
        "buscar_na_web",
        lambda q: [
            RetrievedChunk(
                document=Document(page_content="x", metadata={"source_name": "https://p"}),
                score=0.6,
            )
        ],
    )

    responder.answer(Query(text=PERGUNTA), llm=FakeLLM())

    (reg,) = registros
    assert reg["origem"] == "web"
    assert reg["n_chunks"] == 0  # a base não tinha nada: é o sinal de documento faltando
    assert reg["ms_web"] is not None
    assert reg["web_insuficiente"] is None


def test_encaminhamento_para_a_secretaria_registra_origem_nenhuma(monkeypatch, registros):
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", False)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])

    responder.answer(Query(text=PERGUNTA), llm=FakeLLM())

    (reg,) = registros
    assert (reg["origem"], reg["n_chunks"]) == ("nenhuma", 0)
    assert reg["input_tokens"] is None  # guardrail: nenhum token gasto


def test_falha_no_meio_do_fluxo_ainda_emite_o_registro(monkeypatch, registros):
    """O caso em que a telemetria mais importa não pode ser o que não registra."""

    def explode(_query):
        raise RuntimeError("pgvector fora do ar")

    monkeypatch.setattr(responder, "retrieve", explode)

    with pytest.raises(RuntimeError):
        responder.answer(Query(text=PERGUNTA), llm=FakeLLM())

    (reg,) = registros
    assert reg["erro"] == "RuntimeError: pgvector fora do ar"
    assert reg["origem"] is None
    assert reg["ms_total"] is not None


def test_llm_sem_usage_metadata_nao_quebra(monkeypatch, registros):
    """Provedor que não reporta uso (ou dublê de teste): campos ficam nulos."""

    class SemUso:
        def invoke(self, mensagens):
            return AIMessage(content="ok")

    monkeypatch.setattr(responder, "retrieve", lambda q: [_chunk()])

    responder.answer(Query(text=PERGUNTA), llm=SemUso())

    (reg,) = registros
    assert reg["input_tokens"] is None and reg["ms_llm"] is not None
