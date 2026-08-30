"""Testes do medidor de distribuição de chunk (scripts/chunk_stats.py — RET-5).

Sem loaders reais nem tokenizer: o que precisa ser travado é a aritmética do
relatório (percentis, estimativa de token, simulação de split), não a leitura de
PDF.
"""

from scripts import chunk_stats


def test_percentis_de_lista_conhecida():
    d = chunk_stats._percentis(list(range(1, 101)))  # 1..100

    assert d["min"] == 1
    assert d["max"] == 100
    assert d["p50"] == 50.5
    assert d["p90"] == 90.1
    assert d["media"] == 50.5


def test_percentis_vazio_nao_quebra():
    assert chunk_stats._percentis([]) == {}


def test_tokens_sem_tokenizer_usa_estimativa():
    texto = "a" * 390
    assert chunk_stats._tokens(texto, None) == round(390 / chunk_stats._CHARS_POR_TOKEN)


def test_split_nunca_junta_paginas():
    # duas páginas curtas: mesmo com chunk_size grande, saem 2 pedaços (uma por
    # página), nunca 1 — igual chunker.split_documents
    pedacos = chunk_stats._split(["primeira página curta.", "segunda página curta."], 10_000)
    assert len(pedacos) == 2


def test_split_quebra_pagina_grande_com_chunk_size_menor():
    pagina = "palavra " * 400  # 3200 chars
    assert len(chunk_stats._split([pagina], 5000)) == 1
    assert len(chunk_stats._split([pagina], 500)) > 3
