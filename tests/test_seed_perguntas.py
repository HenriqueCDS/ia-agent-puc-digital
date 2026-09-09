"""Testes de `scripts/seed_perguntas.py` — sem Postgres.

O UPSERT de verdade (idempotência via `(grupo, pergunta_hash)`) é do
`perguntas_store` e não dá para exercitar sem banco; aqui trava-se o que o
script faz por si: ler o JSONC (reusando `_carregar_dataset`), validar cada
item no `--dry-run` sem escrever, e repassar a lista ao store.
"""

import json

from typer.testing import CliRunner

from app.db import perguntas_store
from scripts import seed_perguntas

_DATASET = [
    {"grupo": "teste", "pergunta": "Como envio uma atividade?", "origem_esperada": "base"},
    {"grupo": "teste2", "pergunta": "Como envio uma atividade?", "origem_esperada": "base",
     "criterio": "regressão"},
    {"grupo": "teste", "pergunta": "Qual o calendário?", "origem_esperada": "web"},
]


def _escrever(tmp_path):
    d = tmp_path / "perguntas.jsonc"
    d.write_text("[\n  // bloco\n" + ",\n".join(json.dumps(i) for i in _DATASET) + "\n]", encoding="utf-8")
    return d


def test_dry_run_nao_escreve(tmp_path, monkeypatch):
    chamou = []
    monkeypatch.setattr(perguntas_store, "upsert_muitos", lambda *a, **k: chamou.append(1))

    r = CliRunner().invoke(seed_perguntas.app, ["-a", str(_escrever(tmp_path)), "--dry-run"])

    assert r.exit_code == 0, r.output
    assert "nada foi escrito" in r.output
    assert chamou == []


def test_dry_run_aponta_item_invalido(tmp_path):
    d = tmp_path / "d.jsonc"
    d.write_text(json.dumps([{"pergunta": "x?", "origem_esperada": "chutada"}]), encoding="utf-8")

    r = CliRunner().invoke(seed_perguntas.app, ["-a", str(d), "--dry-run"])

    assert r.exit_code == 0, r.output
    assert "origem_esperada inválida" in r.output


def test_seed_repassa_a_lista_ao_store(tmp_path, monkeypatch):
    recebido = {}

    def fake_upsert(itens, store=None):
        recebido["itens"] = itens
        return perguntas_store.ResumoUpsert(inseridos=3)

    monkeypatch.setattr(perguntas_store, "upsert_muitos", fake_upsert)

    r = CliRunner().invoke(seed_perguntas.app, ["--arquivo", str(_escrever(tmp_path))])

    assert r.exit_code == 0, r.output
    assert len(recebido["itens"]) == 3
    assert "inseridos=3" in r.output


def test_seed_reporta_nada_a_fazer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        perguntas_store, "upsert_muitos",
        lambda *a, **k: perguntas_store.ResumoUpsert(inalterados=3),
    )
    r = CliRunner().invoke(seed_perguntas.app, ["--arquivo", str(_escrever(tmp_path))])
    assert "já estava em dia" in r.output


def test_arquivo_inexistente(tmp_path):
    r = CliRunner().invoke(seed_perguntas.app, ["--arquivo", str(tmp_path / "nao_existe.jsonc")])
    assert r.exit_code != 0
