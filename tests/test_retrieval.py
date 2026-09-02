"""Testes do retrieval, com um vector store falso (sem banco, sem API)."""

import pytest
from langchain_core.documents import Document

from app.core.config import settings
from app.core.models import Query, RetrievedChunk
from app.retrieval.retriever import retrieve


class FakeStore:
    """Dublê de PGVector: registra o filtro e o `k` recebidos e devolve fixos."""

    def __init__(self, resultados):
        self.resultados = resultados
        self.filtro_recebido = "não chamado"
        self.k_recebido = None

    def similarity_search_with_relevance_scores(self, query, k, filter=None):
        self.filtro_recebido = filter
        self.k_recebido = k
        return self.resultados[:k]


def _resultado(texto, score, **meta):
    return Document(page_content=texto, metadata=meta), score


def test_corta_resultados_abaixo_do_limiar(monkeypatch):
    # Este teste é do caminho bi-encoder (corte por `RELEVANCE_THRESHOLD`);
    # fixa o setting para não depender do `.env` de quem roda a suíte.
    monkeypatch.setattr(settings, "reranker_enabled", False)
    store = FakeStore(
        [
            _resultado("relevante", settings.relevance_threshold + 0.2),
            _resultado("irrelevante", settings.relevance_threshold - 0.2),
        ]
    )

    chunks = retrieve(Query(text="como envio atividade?"), store=store)

    assert [c.document.page_content for c in chunks] == ["relevante"]


def test_sem_resultado_relevante_retorna_lista_vazia(monkeypatch):
    monkeypatch.setattr(settings, "reranker_enabled", False)
    store = FakeStore([_resultado("ruído", 0.01)])

    assert retrieve(Query(text="pergunta fora da base"), store=store) == []


def test_assunto_vira_filtro_de_metadata():
    store = FakeStore([])

    retrieve(Query(text="oi", assunto="canvas"), store=store)

    assert store.filtro_recebido == {"assunto": {"$eq": "canvas"}}


def test_sem_assunto_busca_em_toda_a_base():
    store = FakeStore([])

    retrieve(Query(text="oi"), store=store)

    assert store.filtro_recebido is None


@pytest.mark.parametrize(
    "meta,esperado",
    [
        ({"source_name": "guia.pdf", "page": 4}, "guia.pdf, p. 5"),
        ({"source_name": "faq.md"}, "faq.md"),
        ({}, "desconhecido"),
    ],
)
def test_citacao_formata_arquivo_e_pagina(meta, esperado):
    chunk = RetrievedChunk(document=Document(page_content="x", metadata=meta), score=0.9)

    assert chunk.citation == esperado


# --- Reranker cross-encoder (RET-3) -----------------------------------------


@pytest.fixture
def reranker_ligado(monkeypatch):
    """Liga o reranker e substitui `rerank` por um dublê que inverte a ordem.

    Inverter é o suficiente para provar que `retrieve` de fato passou os chunks
    pelo 2º estágio: o dublê reescreve `.score` para uma escala própria e devolve
    a lista ao contrário. O modelo real nunca é carregado.

    `relevance_threshold=0.0` aqui de propósito: o piso de E5 no 1º estágio
    (RET-7) tem teste próprio; estes testes são só sobre o 2º estágio.
    """
    monkeypatch.setattr(settings, "reranker_enabled", True)
    monkeypatch.setattr(settings, "reranker_candidates", 30)
    monkeypatch.setattr(settings, "reranker_threshold", 0.6)
    monkeypatch.setattr(settings, "relevance_threshold", 0.0)

    def fake_rerank(pergunta, chunks):
        # inverte a ordem do E5 e dá scores decrescentes por posição — o
        # bastante para provar que `retrieve` passou pelo 2º estágio.
        invertidos = list(reversed(chunks))
        return [
            RetrievedChunk(document=c.document, score=1.0 - i * 0.25, score_bruto=c.score)
            for i, c in enumerate(invertidos)
        ]

    # `retrieve` faz `from app.retrieval.reranker import rerank` em tempo de
    # chamada — patchar o atributo do módulo basta, o modelo real nunca carrega.
    monkeypatch.setattr("app.retrieval.reranker.rerank", fake_rerank)
    return fake_rerank


def test_reranker_desligado_usa_top_k_e_limiar_do_e5(monkeypatch):
    monkeypatch.setattr(settings, "reranker_enabled", False)
    store = FakeStore([_resultado(f"c{i}", 0.9) for i in range(10)])

    chunks = retrieve(Query(text="x"), store=store)

    assert store.k_recebido == settings.top_k
    assert len(chunks) == settings.top_k
    assert all(c.score_bruto is None for c in chunks)


def test_reranker_ligado_busca_candidatos_reordena_e_corta(monkeypatch, reranker_ligado):
    # 6 candidatos com score de E5 crescente; o dublê inverte, então o antigo
    # último (c5) vira o topo. `reranker_threshold=0.6` corta do 3º em diante
    # (scores do dublê por posição: 1.0, 0.75, 0.5, ...).
    store = FakeStore([_resultado(f"c{i}", 0.80 + i * 0.01) for i in range(6)])

    chunks = retrieve(Query(text="x"), store=store)

    assert store.k_recebido == 30
    assert [c.document.page_content for c in chunks] == ["c5", "c4"]
    assert chunks[0].score_bruto == pytest.approx(0.85)  # score de E5 preservado


def test_reranker_ligado_respeita_top_k(monkeypatch, reranker_ligado):
    monkeypatch.setattr(settings, "reranker_threshold", -1.0)  # não corta nada
    store = FakeStore([_resultado(f"c{i}", 0.9) for i in range(20)])

    chunks = retrieve(Query(text="x"), store=store)

    assert len(chunks) == settings.top_k


# --- INF-9: latência do 2º estágio isolada em `telemetry.ms_rerank` ---------


def test_ms_rerank_e_medido_quando_o_reranker_roda_dentro_de_answer(reranker_ligado):
    """Dentro de `answer()` (registro aberto por `telemetry.registrar`), o custo
    do cross-encoder some para `ms_rerank` — a métrica que o A/B do RET-3 pede."""
    from app.core import telemetry

    store = FakeStore([_resultado(f"c{i}", 0.9) for i in range(6)])

    with telemetry.registrar(assunto=None, pergunta="x", chat_model="m") as registro:
        retrieve(Query(text="x"), store=store)

    assert registro.ms_rerank is not None and registro.ms_rerank >= 0


def test_ms_rerank_fica_nulo_com_o_reranker_desligado(monkeypatch):
    from app.core import telemetry

    monkeypatch.setattr(settings, "reranker_enabled", False)
    store = FakeStore([_resultado("c", 0.9)])

    with telemetry.registrar(assunto=None, pergunta="x", chat_model="m") as registro:
        retrieve(Query(text="x"), store=store)

    assert registro.ms_rerank is None


def test_retrieve_com_reranker_nao_quebra_fora_de_answer(reranker_ligado):
    """Sem registro aberto (ingestão, teste de unidade), `telemetry.etapa` é
    no-op — o rerank roda igual, só não é cronometrado."""
    store = FakeStore([_resultado(f"c{i}", 0.9) for i in range(6)])

    chunks = retrieve(Query(text="x"), store=store)

    assert chunks  # rodou normal, sem exceção


# --- RET-7: o piso de E5 vale no 1º estágio, mesmo com o reranker ligado ----


def test_reranker_ligado_ainda_corta_pelo_piso_de_e5_antes_do_rerank(monkeypatch):
    """RET-7: com `RERANKER_THRESHOLD=0.0` (não calibrado), um chunk que o E5
    pontua abaixo de `RELEVANCE_THRESHOLD` não pode passar só por ter sido
    reordenado — senão 'reranker ligado' seria PIOR que o bi-encoder para lixo
    fora de domínio (Q4 fotossíntese)."""
    monkeypatch.setattr(settings, "reranker_enabled", True)
    monkeypatch.setattr(settings, "reranker_candidates", 30)
    monkeypatch.setattr(settings, "reranker_threshold", 0.0)  # não corta nada
    monkeypatch.setattr(settings, "relevance_threshold", 0.85)

    recebidos_pelo_rerank = {}

    def fake_rerank(pergunta, chunks):
        recebidos_pelo_rerank["textos"] = [c.document.page_content for c in chunks]
        return [
            RetrievedChunk(document=c.document, score=0.99, score_bruto=c.score)
            for c in chunks
        ]

    monkeypatch.setattr("app.retrieval.reranker.rerank", fake_rerank)
    store = FakeStore(
        [_resultado("no_dominio", 0.87), _resultado("fora_do_dominio", 0.82)]
    )

    chunks = retrieve(Query(text="fotossíntese em plantas C4"), store=store)

    # o chunk abaixo de 0.85 nem chegou ao cross-encoder
    assert recebidos_pelo_rerank["textos"] == ["no_dominio"]
    assert [c.document.page_content for c in chunks] == ["no_dominio"]


def test_reranker_ligado_sem_candidato_acima_do_piso_devolve_vazio(monkeypatch):
    monkeypatch.setattr(settings, "reranker_enabled", True)
    monkeypatch.setattr(settings, "relevance_threshold", 0.85)

    def rerank_proibido(*_a, **_k):
        raise AssertionError("rerank não deve ser chamado sem candidato acima do piso")

    monkeypatch.setattr("app.retrieval.reranker.rerank", rerank_proibido)
    store = FakeStore([_resultado("lixo", 0.80), _resultado("mais lixo", 0.79)])

    assert retrieve(Query(text="x"), store=store) == []


# --- RET-8: rerank que levanta exceção não derruba o /ask ------------------


def test_reranker_que_levanta_excecao_cai_para_a_ordem_bi_encoder(monkeypatch, caplog):
    """RET-8: o cross-encoder pode estourar memória na VM. `retrieve` degrada
    para a ordem bi-encoder (já filtrada pelo piso de E5), sem propagar e sem
    marcar `reranker_aplicado` — mesmo espírito da `ProviderChain`."""
    monkeypatch.setattr(settings, "reranker_enabled", True)
    monkeypatch.setattr(settings, "reranker_candidates", 30)
    monkeypatch.setattr(settings, "relevance_threshold", 0.0)

    def rerank_que_estoura(pergunta, chunks):
        raise MemoryError("cross-encoder OOM na VM")

    monkeypatch.setattr("app.retrieval.reranker.rerank", rerank_que_estoura)
    # E5 devolve em ordem decrescente; a fallback preserva essa ordem e corta em top_k
    store = FakeStore([_resultado(f"c{i}", 0.9 - i * 0.01) for i in range(10)])

    with caplog.at_level("WARNING"):
        chunks = retrieve(Query(text="x"), store=store)

    assert [c.document.page_content for c in chunks] == [f"c{i}" for i in range(settings.top_k)]
    assert all(c.score_bruto is None for c in chunks)  # não rerankeou
    assert "reranker falhou" in caplog.text
