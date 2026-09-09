"""Testes de `/v1/perguntas` (app/api/routers/perguntas.py).

O `perguntas_store` é dublado — sem Postgres. O que se trava: leitura vale para
qualquer chave autenticada; ESCRITA só para o consumidor de avaliação (nunca a
demo, cuja chave é pública no HTML); erro no envelope padrão; kill switch.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.core.config import settings
from app.db import perguntas_store

CHAVE_AVAL = "chave-de-avaliacao"
CHAVE_DEMO = "chave-publica-da-demo"


def _p(id_=1, **kw):
    base = dict(
        id=id_, grupo="teste", pergunta="Como envio uma atividade?", pergunta_hash="abc123",
        assunto=None, origem_esperada="base", origem_tambem_ok=[], criterio=None, ativo=True,
    )
    base.update(kw)
    return perguntas_store.PerguntaExemplo(**base)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", f"avaliacao:{CHAVE_AVAL},demo:{CHAVE_DEMO}")
    monkeypatch.setattr(settings, "perguntas_crud_enabled", True)
    monkeypatch.setattr(settings, "perguntas_consumidor_escrita", "avaliacao")
    return TestClient(app_module.create_app(), raise_server_exceptions=False)


def _aval(client):
    return {"X-API-Key": CHAVE_AVAL}


# --- leitura --------------------------------------------------------------


def test_listar_vale_para_qualquer_chave_autenticada(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "listar", lambda **k: [_p(1), _p(2)])
    r = client.get("/v1/perguntas", headers={"X-API-Key": CHAVE_DEMO})
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert r.json()["perguntas"][0]["pergunta_hash"] == "abc123"


def test_listar_sem_chave_e_401(client):
    assert client.get("/v1/perguntas").status_code == 401


def test_obter_404_no_envelope(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "obter", lambda id_, store=None: None)
    r = client.get("/v1/perguntas/99", headers={"X-API-Key": CHAVE_DEMO})
    assert r.status_code == 404
    assert r.json()["erro"] == "nao_encontrado"
    assert "request_id" in r.json()


# --- escrita: só o consumidor de avaliação -------------------------------


def test_demo_nao_pode_criar(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "criar", lambda *a, **k: _p())
    r = client.post(
        "/v1/perguntas",
        headers={"X-API-Key": CHAVE_DEMO},
        json={"pergunta": "Nova pergunta?", "origem_esperada": "base"},
    )
    assert r.status_code == 403
    assert r.json()["erro"] == "sem_permissao"


def test_avaliacao_cria(client, monkeypatch):
    capturado = {}
    monkeypatch.setattr(
        perguntas_store, "criar",
        lambda item, store=None: capturado.update(item) or _p(id_=7, grupo="teste2"),
    )
    r = client.post(
        "/v1/perguntas",
        headers=_aval(client),
        json={"pergunta": "Nova pergunta?", "origem_esperada": "web", "grupo": "teste2",
              "origem_tambem_ok": ["nenhuma"]},
    )
    assert r.status_code == 201
    assert r.json()["id"] == 7
    assert capturado["origem_tambem_ok"] == ["nenhuma"]


def test_create_valida_origem_pelo_schema(client):
    r = client.post(
        "/v1/perguntas",
        headers=_aval(client),
        json={"pergunta": "Pergunta ok?", "origem_esperada": "inventada"},
    )
    assert r.status_code == 422
    assert r.json()["erro"] == "validacao"


def test_patch_parcial_so_manda_o_que_veio(client, monkeypatch):
    capturado = {}

    def fake_atualizar(id_, campos, store=None):
        capturado.update(campos)
        return _p(id_=id_, origem_esperada="nenhuma")

    monkeypatch.setattr(perguntas_store, "atualizar", fake_atualizar)
    r = client.patch("/v1/perguntas/3", headers=_aval(client), json={"origem_esperada": "nenhuma"})
    assert r.status_code == 200
    assert capturado == {"origem_esperada": "nenhuma"}  # `criterio`/`grupo` não vão junto


def test_patch_404(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "atualizar", lambda *a, **k: None)
    r = client.patch("/v1/perguntas/9", headers=_aval(client), json={"criterio": "x"})
    assert r.status_code == 404


def test_delete_e_logico_e_idempotente(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "obter", lambda id_, store=None: _p(id_=id_))
    chamou = []
    monkeypatch.setattr(perguntas_store, "desativar", lambda id_, store=None: chamou.append(id_))
    r = client.delete("/v1/perguntas/4", headers=_aval(client))
    assert r.status_code == 204
    assert chamou == [4]


def test_delete_404_sem_a_pergunta(client, monkeypatch):
    monkeypatch.setattr(perguntas_store, "obter", lambda id_, store=None: None)
    assert client.delete("/v1/perguntas/4", headers=_aval(client)).status_code == 404


# --- kill switch --------------------------------------------------------


def test_desligado_tira_a_rota_do_ar(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "perguntas_crud_enabled", False)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", f"avaliacao:{CHAVE_AVAL}")
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)

    assert client.get("/v1/perguntas", headers={"X-API-Key": CHAVE_AVAL}).status_code == 404
    assert client.get("/v1/health").status_code == 200
