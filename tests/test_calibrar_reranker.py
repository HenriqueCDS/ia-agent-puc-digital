"""Testes do calibrador de RERANKER_THRESHOLD (scripts/calibrar_reranker.py — RET-7).

Sem eval_run real: o que precisa ser travado é a redução por pergunta (mediana
de N rodadas), a separação em grupos (cobre-base × alvo-de-corte) e a varredura
que escolhe o maior T sem quebrar acerto real.
"""

import json

import pytest
from typer.testing import CliRunner

from scripts import calibrar_reranker

runner = CliRunner()


def _linha(pergunta, esperada, obtida, score_top, acertou=None, **extra):
    if acertou is None:
        acertou = obtida in (esperada, *extra.get("origem_tambem_ok", ()))
    return {
        "pergunta": pergunta,
        "origem_esperada": esperada,
        "origem_obtida": obtida,
        "acertou": acertou,
        "score_top": score_top,
        "score_top_bruto": extra.get("bruto"),
        "reranker_aplicado": extra.get("reranker_aplicado", True),
    }


def _rodar(tmp_path, linhas, *args):
    arquivo = tmp_path / "run.json"
    arquivo.write_text(json.dumps(linhas), encoding="utf-8")
    return runner.invoke(calibrar_reranker.app, [str(arquivo), *args])


def test_mediana_por_pergunta_reduz_as_rodadas():
    linhas = [
        _linha("q1", "base", "base", 0.40),
        _linha("q1", "base", "base", 0.60),
        _linha("q1", "base", "base", 0.50),
    ]
    perguntas = calibrar_reranker._por_pergunta(linhas)

    assert perguntas["q1"]["score_top"] == 0.50
    assert perguntas["q1"]["n_rodadas"] == 3


def test_recomenda_o_maior_t_que_nao_quebra_a_base(tmp_path):
    # base cobre com score 0.70/0.80; vazamento fora de domínio a 0.30/0.45.
    # T entre 0.45 e 0.70 corta os 2 vazamentos e não quebra base.
    linhas = [
        _linha("cobre A", "base", "base", 0.70),
        _linha("cobre B", "base", "base", 0.80),
        _linha("fotossintese", "nenhuma", "base", 0.30),
        _linha("capital da frança", "encaminhado", "base", 0.45),
    ]
    resultado = _rodar(tmp_path, linhas)

    assert resultado.exit_code == 0
    # maior múltiplo de 0.02 estritamente acima de 0.45 e não acima de 0.70:
    # a base mais baixa é 0.70, então T=0.70 não quebra (corte é `< T`).
    assert "RERANKER_THRESHOLD = 0.70" in resultado.stdout
    assert "corta 2 vazamento" in resultado.stdout


def test_faixas_sobrepostas_manda_para_a_defesa_a(tmp_path):
    # vazamento a 0.65 pontua ACIMA da base mais fraca (0.55) -> sem T limpo.
    linhas = [
        _linha("cobre fraca", "base", "base", 0.55),
        _linha("cobre forte", "base", "base", 0.90),
        _linha("lixo bem pontuado", "nenhuma", "base", 0.65),
    ]
    resultado = _rodar(tmp_path, linhas)

    assert resultado.exit_code == 1
    assert "defesa (a)" in resultado.stdout


def test_rejeita_rodada_sem_reranker_aplicado(tmp_path):
    linhas = [_linha("q", "base", "base", 0.8, reranker_aplicado=False)]
    resultado = _rodar(tmp_path, linhas)

    assert resultado.exit_code != 0
    assert "RERANKER_ENABLED=false" in resultado.stdout


def test_tolerancia_permite_quebrar_n_perguntas(tmp_path):
    # uma base a 0.20 (outlier); com tolerância 1, T pode subir acima dela.
    linhas = [
        _linha("cobre outlier", "base", "base", 0.20),
        _linha("cobre normal", "base", "base", 0.75),
        _linha("lixo", "nenhuma", "base", 0.50),
    ]
    sem_tol = _rodar(tmp_path, linhas)
    assert sem_tol.exit_code == 1  # T teria que ficar < 0.20, não corta o lixo

    com_tol = _rodar(tmp_path, linhas, "--tolerancia", "1")
    assert com_tol.exit_code == 0
    assert "quebra 1 base" in com_tol.stdout
