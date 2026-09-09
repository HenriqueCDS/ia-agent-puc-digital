"""Testes da rota de revisão (app/api/routers/revisao.py) — agora sobre o banco.

O store (`revisao_store` / `perguntas_store`) é dublado: o que se trava aqui é o
contrato da rota e a AGREGAÇÃO do dashboard (`_resumo`), que é lógica pura. A
página continua sem poder puxar recurso externo.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.api.routers import revisao as revisao_router
from app.core.config import settings
from app.db import perguntas_store, revisao_store


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
    return revisao_store.LinhaRevisao(**base)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "revisao_enabled", True)
    monkeypatch.setattr(perguntas_store, "grupos", lambda store=None: ["teste", "teste2"])
    return TestClient(app_module.create_app(), raise_server_exceptions=False)


def test_serve_o_html(client):
    r = client.get("/revisao")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Revisão de Avaliação" in r.text


def test_html_nao_referencia_recurso_externo():
    """Chart.js vem de /revisao/chart.js (mesma origem, vendorizado), nunca de
    CDN — a página abre sem internet, como a demo."""
    html = revisao_router._PAGINA.read_text(encoding="utf-8")
    assert re.findall(r'(?:src|href)="((?:https?:)?//[^"]+)"', html) == []
    assert 'src="/revisao/chart.js"' in html


def test_serve_o_chart_js_vendorizado(client):
    r = client.get("/revisao/chart.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "Chart" in r.text and len(r.content) > 50_000  # o bundle inteiro
    assert r.headers.get("cache-control") == "public, max-age=86400"


def test_dados_devolve_resumo_e_linhas(client, monkeypatch):
    linhas = [
        _linha(pergunta_hash="a", grupo="teste", origem_esperada="base", origem_obtida="base"),
        _linha(pergunta_hash="b", grupo="teste", origem_esperada="web", origem_obtida="nenhuma",
               telemetria_id=11, veredicto="insatisfeito"),
        _linha(pergunta_hash="c", grupo="teste2", telemetria_id=None, origem_obtida=None),
    ]
    monkeypatch.setattr(revisao_router.revisao_store, "linhas_da_rodada", lambda **k: linhas)

    corpo = client.get("/revisao/dados").json()

    assert corpo["resumo"]["total"] == 3
    assert corpo["resumo"]["executadas"] == 2
    assert corpo["resumo"]["nao_executadas"] == 1
    assert corpo["resumo"]["acerto_geral"] == {"acertou": 1, "avaliaveis": 2, "taxa": 0.5}
    assert corpo["resumo"]["veredictos"]["insatisfeito"] == 1
    assert corpo["filtros"]["grupos_disponiveis"] == ["teste", "teste2"]
    assert [l["pergunta_hash"] for l in corpo["linhas"]] == ["a", "b", "c"]


def test_resumo_cruza_roteamento_e_fidelidade():
    linhas = [
        _linha(origem_esperada="base", origem_obtida="base", veredicto="insatisfeito"),   # certo × insat
        _linha(origem_esperada="base", origem_obtida="web", veredicto="satisfeito"),      # divergiu × sat
        _linha(origem_esperada="base", origem_obtida="base", veredicto="satisfeito"),     # certo × sat
    ]
    cruz = revisao_router._resumo(linhas)["roteamento_x_fidelidade"]
    assert cruz == {"certo_sat": 1, "certo_insat": 1, "divergiu_sat": 1, "divergiu_insat": 0}


def test_resumo_percentil_de_latencia():
    linhas = [_linha(ms_total=v) for v in (100, 200, 300, 400, 500)]
    lat = revisao_router._resumo(linhas)["latencia_ms"]
    assert lat["p50"] == 300 and lat["media"] == 300
    assert lat["p95"] == 480


def test_resumo_flags_contam_do_jsonb_de_telemetria():
    linhas = [
        _linha(telemetria={"pii": ["cpf"], "base_insuficiente": True, "reranker_aplicado": True}),
        _linha(telemetria={"pii": None, "base_insuficiente": False, "reranker_aplicado": True}),
        _linha(telemetria={"erro": "Timeout", "reranker_aplicado": None}),
    ]
    r = revisao_router._resumo(linhas)
    assert r["flags"]["pii"] == {"sim": 1, "nao": 2, "indef": 0}
    assert r["flags"]["erro"] == {"sim": 1, "nao": 2, "indef": 0}
    assert r["flags"]["base_insuficiente"] == {"sim": 1, "nao": 1, "indef": 1}
    assert r["flags"]["reranker_aplicado"] == {"sim": 2, "nao": 0, "indef": 1}
    assert r["pii_categorias"] == {"cpf": 1}


def test_resumo_topicos_e_fluxo():
    linhas = [
        _linha(telemetria={"topico": "Transferência externa", "ms_rerank": 300.0, "ms_web": 8000.0},
               ms_total=17000.0, ms_retrieve=6000.0, ms_llm=2700.0),
        _linha(telemetria={"topico": "Transferência externa"}),
        _linha(telemetria={"topico": "Fórum avaliativo"}),
    ]
    r = revisao_router._resumo(linhas)
    assert r["topicos"][0] == ["Transferência externa", 2]
    assert len(r["fluxo"]) == 3
    # ms_retrieve no fluxo já desconta o rerank (1º estágio isolado).
    assert r["fluxo"][0]["ms_retrieve"] == 5700.0
    assert r["tempo_por_etapa"]["ms_web"] == 8000.0  # média só das que têm web


def test_grava_veredicto(client, monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        revisao_router.revisao_store, "salvar_veredicto",
        lambda *a, **k: chamadas.append((a, k)),
    )
    r = client.put("/revisao/veredicto", json={
        "telemetria_id": 5, "pergunta_hash": "h1", "veredicto": "satisfeito",
    })
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert chamadas == [((5, "h1", "satisfeito", None), {})]


def test_veredicto_invalido_vira_422(client, monkeypatch):
    chamou = []
    monkeypatch.setattr(
        revisao_router.revisao_store, "salvar_veredicto", lambda *a, **k: chamou.append(1)
    )
    r = client.put("/revisao/veredicto", json={
        "telemetria_id": 5, "pergunta_hash": "h1", "veredicto": "x",
    })
    assert r.status_code == 422
    assert r.json()["erro"] == "veredicto_invalido"
    assert chamou == []  # nem chega ao store


def test_ajustar_expectativa_chama_o_store(client, monkeypatch):
    p = perguntas_store.PerguntaExemplo(
        id=3, grupo="teste", pergunta="P?", pergunta_hash="h", assunto=None,
        origem_esperada="nenhuma", origem_tambem_ok=["encaminhado"], criterio=None, ativo=True,
    )
    monkeypatch.setattr(revisao_router.perguntas_store, "atualizar", lambda id_, campos, store=None: p)

    r = client.patch("/revisao/pergunta/3", json={"origem_esperada": "nenhuma"})
    assert r.status_code == 200
    assert r.json()["origem_esperada"] == "nenhuma"
    assert r.json()["origem_tambem_ok"] == ["encaminhado"]


def test_ajustar_expectativa_404_sem_a_pergunta(client, monkeypatch):
    monkeypatch.setattr(revisao_router.perguntas_store, "atualizar", lambda *a, **k: None)
    assert client.patch("/revisao/pergunta/999", json={"criterio": "x"}).status_code == 404


def test_desligada_tira_a_rota_do_ar(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "revisao_enabled", False)
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)

    assert client.get("/revisao").status_code == 404
    assert client.get("/v1/health").status_code == 200
