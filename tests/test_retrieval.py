"""Testes do retrieval e do guardrail, com um vector store falso (sem banco, sem API)."""

import pytest
from langchain_core.documents import Document

from app.core.config import settings
from app.core.models import Query, RetrievedChunk
from app.retrieval.retriever import retrieve


class FakeStore:
    """Dublê de PGVector: registra o filtro recebido e devolve resultados fixos."""

    def __init__(self, resultados):
        self.resultados = resultados
        self.filtro_recebido = "não chamado"

    def similarity_search_with_relevance_scores(self, query, k, filter=None):
        self.filtro_recebido = filter
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
