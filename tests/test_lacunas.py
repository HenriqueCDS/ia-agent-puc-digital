"""Testes do relatório de lacunas (T3.3, scripts/lacunas.py).

Sem banco, como o resto da suíte: `consultar_lacunas`/`contar_perguntas` são
dublados e o que se testa é a leitura do relatório — a priorização, o rótulo de
cada situação e o JSON. A consulta em si é SQL, e travá-la aqui exigiria um
Postgres; ela foi validada contra o banco real (ver BACKLOG.md, Sprint 3).
"""

import json
from datetime import datetime, timezone

from typer.testing import CliRunner

from app.db.telemetry_store import Lacuna
from scripts import lacunas as script
from scripts.lacunas import app

runner = CliRunner()
AGORA = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _lacuna(rotulo, ocorrencias=1, sem_resposta=1, distintas=1, assuntos="canvas"):
    return Lacuna(
        rotulo=rotulo,
        assuntos=assuntos,
        ocorrencias=ocorrencias,
        perguntas_distintas=distintas,
        sem_resposta=sem_resposta,
        ultima_vez=AGORA,
    )


def _dublar(monkeypatch, itens, total=10):
    monkeypatch.setattr(script, "consultar_lacunas", lambda dias, limite: itens[:limite])
    monkeypatch.setattr(script, "contar_perguntas", lambda dias: (total, len(itens)))


def test_lista_as_lacunas_com_o_tema_e_a_frequencia(monkeypatch):
    _dublar(monkeypatch, [_lacuna("envio de atividade com prazo expirado", ocorrencias=7)])

    resultado = runner.invoke(app, ["--dias", "7"])

    assert resultado.exit_code == 0
    assert "envio de atividade com prazo expirado" in resultado.output
    assert "7" in resultado.output


def test_sem_resposta_vem_antes_do_que_a_web_cobriu(monkeypatch):
    """A ordem é a de prioridade: quem foi para a secretaria primeiro. A
    ordenação real é do SQL — aqui se garante que o relatório não a reembaralha."""
    _dublar(monkeypatch, [
        _lacuna("tema grave", ocorrencias=2, sem_resposta=2),
        _lacuna("tema coberto pela web", ocorrencias=9, sem_resposta=0),
    ])

    saida = runner.invoke(app, []).output

    assert saida.index("tema grave") < saida.index("tema coberto pela web")


def test_situacao_distingue_sem_resposta_de_coberta_pela_web(monkeypatch):
    """Cada caso exige uma ação diferente de quem cuida do conteúdo: um manda o
    aluno para a secretaria, o outro só troca RAG rápido por busca externa lenta."""
    _dublar(monkeypatch, [
        _lacuna("nada respondeu", ocorrencias=3, sem_resposta=3),
        _lacuna("web respondeu", ocorrencias=3, sem_resposta=0),
        _lacuna("as vezes responde", ocorrencias=4, sem_resposta=1),
    ])

    saida = runner.invoke(app, []).output

    assert "sem resposta      nada respondeu" in saida
    assert "coberta pela web  web respondeu" in saida
    assert "1/4 sem resposta" in saida


def test_proporcao_do_trafego_aparece(monkeypatch):
    """A lista sozinha não distingue 3 lacunas em 10 perguntas de 3 em 3000."""
    _dublar(monkeypatch, [_lacuna("a"), _lacuna("b"), _lacuna("c")], total=10)

    saida = runner.invoke(app, []).output

    assert "10 pergunta(s)" in saida
    assert "3 (30%)" in saida


def test_sem_telemetria_explica_em_vez_de_mostrar_relatorio_vazio(monkeypatch):
    _dublar(monkeypatch, [], total=0)

    saida = runner.invoke(app, []).output

    assert "TELEMETRY_DB_ENABLED" in saida


def test_base_cobrindo_tudo_nao_e_erro(monkeypatch):
    _dublar(monkeypatch, [], total=40)

    saida = runner.invoke(app, []).output

    assert "Nenhuma lacuna" in saida


def test_janela_maior_que_a_retencao_avisa(monkeypatch):
    """Pedir 90 dias de uma tabela que guarda 7 devolveria uma janela quase
    vazia — e isso pareceria "semana tranquila" em vez de dado inexistente."""
    _dublar(monkeypatch, [_lacuna("a")])
    monkeypatch.setattr(script.settings, "telemetry_retention_days", 7)

    resultado = runner.invoke(app, ["--dias", "90"])

    assert "retenção da telemetria é de 7 dias" in resultado.output


def test_saida_json_e_parseavel(monkeypatch):
    """`--json` alimenta pipeline; nenhum aviso ou cor pode contaminar o stdout."""
    _dublar(monkeypatch, [_lacuna("tema x", ocorrencias=4, distintas=2)], total=20)

    # `--dias 90` acima da retenção: o aviso vai para stderr e não pode quebrar
    # o parse. `CliRunner` separa os dois por padrão.
    resultado = runner.invoke(app, ["--dias", "90", "--json"])

    corpo = json.loads(resultado.stdout)
    assert corpo["perguntas"] == 20
    assert corpo["itens"][0] == {
        "tema": "tema x",
        "assuntos": "canvas",
        "ocorrencias": 4,
        "perguntas_distintas": 2,
        "sem_resposta": 1,
        "ultima_vez": AGORA.isoformat(),
    }
