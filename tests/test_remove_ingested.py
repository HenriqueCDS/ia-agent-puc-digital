"""Testes da CLI de remoção (scripts/remove_ingested.py) e dos helpers de delete
em app/db/vector_store.py.

Sem banco: `_assunto_de` é lógica pura, e o resto é testado via `CliRunner`
com `get_vector_store`/`list_ingested_sources`/`delete_by_*` dublados — mesmo
padrão do resto do projeto (nenhum teste toca o Postgres real).
"""

import pytest
from typer.testing import CliRunner

from scripts import remove_ingested
from scripts.remove_ingested import _assunto_de, app

runner = CliRunner()


def test_assunto_de_extrai_a_pasta_apos_raw():
    assert _assunto_de("/proj/data/raw/canvas/guia.pdf") == "canvas"
    assert _assunto_de(r"C:\proj\data\raw\puc-digital\portal.pdf") == "puc-digital"


def test_assunto_de_sem_pasta_raw_devolve_none():
    assert _assunto_de("/algum/outro/caminho.pdf") is None


def test_sem_termo_e_sem_assunto_da_erro(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())

    resultado = runner.invoke(app, [])

    assert resultado.exit_code == 1
    assert "Informe um termo" in resultado.output


def test_termo_e_assunto_juntos_da_erro(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())

    resultado = runner.invoke(app, ["guia.pdf", "--assunto", "canvas"])

    assert resultado.exit_code == 1
    assert "Use --assunto sozinho" in resultado.output


def test_termo_sem_casar_nenhum_arquivo_nao_apaga_nada(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(remove_ingested, "list_ingested_sources", lambda store: [])
    chamado = []
    monkeypatch.setattr(remove_ingested, "delete_by_source", lambda store, p: chamado.append(p))

    resultado = runner.invoke(app, ["nada-a-ver.pdf"])

    assert "Nenhum arquivo indexado" in resultado.output
    assert chamado == []


def test_termo_pede_confirmacao_e_cancela_sem_apagar(monkeypatch):
    """Sem --yes, um 'n' na confirmação não deve remover nada — é a proteção
    contra um termo genérico demais (ex.: ".pdf") apagar mais que o esperado."""
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested,
        "list_ingested_sources",
        lambda store: [("/data/raw/canvas/guia.pdf", 12)],
    )
    chamado = []
    monkeypatch.setattr(remove_ingested, "delete_by_source", lambda store, p: chamado.append(p))

    resultado = runner.invoke(app, ["guia.pdf"], input="n\n")

    assert "Cancelado" in resultado.output
    assert chamado == []


def test_termo_com_yes_remove_sem_perguntar(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested,
        "list_ingested_sources",
        lambda store: [("/data/raw/canvas/guia.pdf", 12), ("/data/raw/canvas/outro.pdf", 3)],
    )
    chamado = []
    monkeypatch.setattr(
        remove_ingested, "delete_by_source", lambda store, p: chamado.append(p) or 5
    )

    resultado = runner.invoke(app, ["guia.pdf", "--yes"])

    assert resultado.exit_code == 0
    assert chamado == ["/data/raw/canvas/guia.pdf"]  # "outro.pdf" não casa com "guia.pdf"
    assert "5 chunk(s) removido(s)" in resultado.output


def test_termo_e_case_insensitive(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested, "list_ingested_sources", lambda store: [("/data/raw/canvas/GUIA.PDF", 1)]
    )
    monkeypatch.setattr(remove_ingested, "delete_by_source", lambda store, p: 1)

    resultado = runner.invoke(app, ["guia.pdf", "--yes"])

    assert resultado.exit_code == 0
    assert "1 chunk(s) removido(s)" in resultado.output


def test_assunto_remove_a_pasta_inteira_via_delete_by_assunto(monkeypatch):
    """--assunto usa delete_by_assunto (uma query), não uma por arquivo."""
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested,
        "list_ingested_sources",
        lambda store: [
            ("/data/raw/canvas/a.pdf", 4),
            ("/data/raw/puc-digital/b.pdf", 2),  # outro assunto: não deve aparecer
        ],
    )
    chamado = []
    monkeypatch.setattr(
        remove_ingested, "delete_by_assunto", lambda store, a: chamado.append(a) or 7
    )

    resultado = runner.invoke(app, ["--assunto", "canvas", "--yes"])

    assert resultado.exit_code == 0
    assert chamado == ["canvas"]
    assert "puc-digital" not in resultado.output
    assert "7 chunk(s) removido(s)" in resultado.output


def test_web_e_assunto_juntos_da_erro(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())

    resultado = runner.invoke(app, ["--web", "--assunto", "canvas"])

    assert resultado.exit_code == 1
    assert "Use --assunto sozinho" in resultado.output


def test_web_sozinho_remove_todo_o_conteudo_crawlado(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested,
        "list_web_sources",
        lambda store: [
            ("https://www.puc-campinas.edu.br/biblioteca/", 4),
            ("https://community.instructure.com/en/kb/articles/1", 2),
        ],
    )
    apagadas = []
    monkeypatch.setattr(
        remove_ingested, "delete_by_source", lambda store, p: apagadas.append(p) or 3
    )

    resultado = runner.invoke(app, ["--web", "--yes"])

    assert resultado.exit_code == 0
    assert apagadas == [
        "https://www.puc-campinas.edu.br/biblioteca/",
        "https://community.instructure.com/en/kb/articles/1",
    ]
    assert "6 chunk(s) removido(s)" in resultado.output


def test_web_com_termo_filtra_por_trecho_da_url(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested,
        "list_web_sources",
        lambda store: [
            ("https://www.puc-campinas.edu.br/biblioteca/", 4),
            ("https://community.instructure.com/en/kb/articles/1", 2),
        ],
    )
    apagadas = []
    monkeypatch.setattr(
        remove_ingested, "delete_by_source", lambda store, p: apagadas.append(p) or 4
    )

    resultado = runner.invoke(app, ["--web", "instructure.com", "--yes"])

    assert apagadas == ["https://community.instructure.com/en/kb/articles/1"]
    assert "puc-campinas" not in resultado.output


def test_web_sem_conteudo_crawlado_nao_apaga(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(remove_ingested, "list_web_sources", lambda store: [])
    monkeypatch.setattr(
        remove_ingested, "delete_by_source", lambda *a: pytest.fail("nada a apagar")
    )

    resultado = runner.invoke(app, ["--web", "--yes"])

    assert "Nenhuma página crawlada" in resultado.output


def test_assunto_sem_arquivos_correspondentes_nao_chama_delete(monkeypatch):
    monkeypatch.setattr(remove_ingested, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        remove_ingested, "list_ingested_sources", lambda store: [("/data/raw/canvas/a.pdf", 4)]
    )
    chamado = []
    monkeypatch.setattr(
        remove_ingested, "delete_by_assunto", lambda store, a: chamado.append(a) or 0
    )

    resultado = runner.invoke(app, ["--assunto", "inexistente", "--yes"])

    assert chamado == []  # nada casou -> não chega a chamar o delete
