"""Testes da rota de revisão manual de fidelidade (app/api/routers/revisao.py).

O que precisa estar travado: o `nome` do arquivo na URL nunca alcança nada fora
de `eval/resultados/` (é o único ponto que lê disco a partir de entrada do
cliente), a página não puxa recurso externo (mesma promessa da demo), e a rota
inteira some com `REVISAO_ENABLED=false`.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.api.routers import revisao as revisao_router
from app.core.config import settings

_LINHAS = [
    {"pergunta": "Como envio uma atividade?", "grupo": "teste", "origem_esperada": "base",
     "origem_obtida": "base", "acertou": True, "resposta": "Passo 1..."},
    {"pergunta": "Qual o calendário?", "grupo": "teste", "origem_esperada": "web",
     "origem_obtida": "nenhuma", "acertou": False, "resposta": "Não encontrei..."},
]


@pytest.fixture
def resultados_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(revisao_router, "_RESULTADOS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(resultados_dir, monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "revisao_enabled", True)
    return TestClient(app_module.create_app(), raise_server_exceptions=False)


def test_serve_o_html(client):
    resposta = client.get("/revisao")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/html")
    assert "<title>Revisão de Avaliação" in resposta.text


def test_html_nao_referencia_recurso_externo():
    """Mesma regressão da demo: nenhum `src`/`href` pode apontar para fora —
    é o que mantém a página abrindo numa máquina sem internet."""
    html = revisao_router._PAGINA.read_text(encoding="utf-8")
    externos = re.findall(r'(?:src|href)="((?:https?:)?//[^"]+)"', html)

    assert externos == []


def test_lista_resultados_mais_recente_primeiro(client, resultados_dir):
    antigo = resultados_dir / "20260101T000000Z.json"
    novo = resultados_dir / "20260902T000000Z.json"
    antigo.write_text("[]", encoding="utf-8")
    novo.write_text("[]", encoding="utf-8")
    import os
    os.utime(antigo, (1_000_000, 1_000_000))
    os.utime(novo, (2_000_000, 2_000_000))

    nomes = [a["nome"] for a in client.get("/revisao/resultados").json()["arquivos"]]

    assert nomes == ["20260902T000000Z.json", "20260101T000000Z.json"]


def test_lista_vazia_sem_o_diretorio(client, resultados_dir):
    resultados_dir.rmdir()

    assert client.get("/revisao/resultados").json() == {"arquivos": []}


def test_obtem_o_array_da_rodada(client, resultados_dir):
    (resultados_dir / "run.json").write_text(json.dumps(_LINHAS), encoding="utf-8")

    resposta = client.get("/revisao/resultados/run.json")

    assert resposta.status_code == 200
    assert resposta.json() == _LINHAS


@pytest.mark.parametrize(
    "nome",
    [
        "../config.py",
        "..%2f..%2f.env",
        "sub/dir.json",
        "run.txt",
        "run.json.bak",
        ".env",
    ],
)
def test_nome_fora_do_diretorio_ou_sem_json_da_404(client, resultados_dir, nome):
    (resultados_dir / "run.json").write_text("[]", encoding="utf-8")

    assert client.get(f"/revisao/resultados/{nome}").status_code == 404


def test_symlink_para_fora_nao_e_servido(client, resultados_dir, tmp_path):
    """A regex deixa passar `escape.json`; a revalidação de path é o que barra —
    o link resolve para fora de `_RESULTADOS_DIR`."""
    segredo = tmp_path / "segredo.txt"
    segredo.write_text("[]", encoding="utf-8")
    link = resultados_dir / "escape.json"
    try:
        link.symlink_to(segredo)
    except (OSError, NotImplementedError):
        pytest.skip("sem permissão para criar symlink neste ambiente")

    assert client.get("/revisao/resultados/escape.json").status_code == 404


def test_json_invalido_vira_422(client, resultados_dir):
    (resultados_dir / "quebrado.json").write_text("{ nao fecha", encoding="utf-8")

    assert client.get("/revisao/resultados/quebrado.json").status_code == 422


def test_json_que_nao_e_lista_vira_422(client, resultados_dir):
    (resultados_dir / "objeto.json").write_text('{"a": 1}', encoding="utf-8")

    assert client.get("/revisao/resultados/objeto.json").status_code == 422


def test_desligada_tira_a_rota_do_ar(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "revisao_enabled", False)
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)

    assert client.get("/revisao").status_code == 404
    # A v1 continua de pé — desligar a revisão não é desligar o serviço.
    assert client.get("/v1/health").status_code == 200
