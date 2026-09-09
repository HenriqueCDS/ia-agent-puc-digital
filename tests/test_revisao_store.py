"""Testes de `app/db/revisao_store.py` — sem Postgres.

O cruzamento dataset × telemetria × veredito acontece em Python (`_montar`,
`linhas_da_rodada`); o que se trava aqui é a lógica dele: `acertou` como a
mesma regra de `eval_run`, e os dois casos de hash sem par nas duas pontas.
"""

import pytest

from app.db import revisao_store as rs
from app.db.perguntas_store import PerguntaExemplo


def _linha(**kw):
    base = dict(
        pergunta_hash="h1", pergunta_id=1, grupo="teste", pergunta="P?", assunto=None,
        origem_esperada="base", origem_tambem_ok=[], criterio=None,
        telemetria_id=10, criado_em=None, origem_obtida="base", grounded=True,
        resposta="R.", provider="gemini", chat_model="x", cache_hit=False,
        score_top=0.9, score_min=0.3, score_mean=0.5, n_chunks=5,
        input_tokens=100, output_tokens=20, ms_total=1000.0, ms_retrieve=200.0,
        ms_llm=700.0, telemetria={}, veredicto=None, nota=None,
    )
    base.update(kw)
    return rs.LinhaRevisao(**base)


def test_acertou_usa_esperada_mais_tambem_ok():
    assert _linha(origem_esperada="web", origem_obtida="nenhuma").acertou is False
    assert _linha(
        origem_esperada="web", origem_tambem_ok=["nenhuma"], origem_obtida="nenhuma"
    ).acertou is True


def test_acertou_indeterminado_sem_um_dos_lados():
    assert _linha(origem_obtida=None).acertou is None          # não executou
    assert _linha(origem_esperada=None).acertou is None        # saiu do dataset


def test_margem_relativa():
    assert _linha(score_top=0.9, score_min=0.3).margem_relativa == 0.6
    assert _linha(score_top=None).margem_relativa is None


def test_montar_pergunta_sem_execucao():
    p = PerguntaExemplo(
        id=7, grupo="teste2", pergunta="Nova?", pergunta_hash="hx", assunto=None,
        origem_esperada="base", origem_tambem_ok=[], criterio="conferir",
        ativo=True,
    )
    linha = rs._montar("hx", p, None, {})
    assert linha.pergunta == "Nova?" and linha.telemetria_id is None
    assert linha.acertou is None
    assert linha.criterio == "conferir"


def test_montar_execucao_orfa_sem_pergunta():
    exec_row = type("R", (), dict(
        telemetria_id=99, criado_em=None, origem_obtida="base", grounded=True,
        resposta="R", provider="groq", chat_model="m", cache_hit=False,
        score_top=None, score_min=None, score_mean=None, n_chunks=None,
        input_tokens=None, output_tokens=None, ms_total=None, ms_retrieve=None, ms_llm=None,
        dados={"origem": "base"},
    ))()
    linha = rs._montar("hy", None, exec_row, {99: ("insatisfeito", "meh")})
    assert linha.pergunta is None and linha.pergunta_id is None
    assert linha.origem_esperada is None
    assert (linha.veredicto, linha.nota) == ("insatisfeito", "meh")


def test_salvar_veredicto_rejeita_valor_invalido(monkeypatch):
    monkeypatch.setattr(rs, "_ensure_table", lambda store=None: None)
    with pytest.raises(ValueError, match="veredicto inválido"):
        rs.salvar_veredicto(1, "h", "gostei")


def test_veredictos_validos_sao_os_tres():
    assert set(rs.VEREDICTOS_VALIDOS) == {"satisfeito", "insatisfeito", "pulado"}
