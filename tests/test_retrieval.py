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


def test_corta_resultados_abaixo_do_limiar():
    store = FakeStore(
        [
            _resultado("relevante", settings.relevance_threshold + 0.2),
            _resultado("irrelevante", settings.relevance_threshold - 0.2),
        ]
    )

    chunks = retrieve(Query(text="como envio atividade?"), store=store)

    assert [c.document.page_content for c in chunks] == ["relevante"]


def test_sem_resultado_relevante_retorna_lista_vazia():
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
    """
    monkeypatch.setattr(settings, "reranker_enabled", True)
    monkeypatch.setattr(settings, "reranker_candidates", 30)
    monkeypatch.setattr(settings, "reranker_threshold", 0.6)

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
