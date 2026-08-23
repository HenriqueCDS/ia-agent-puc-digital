"""Testes do frontend de demonstração (T3.1) — a rota, não o HTML.

O que precisa estar travado aqui é o que separa "demo funcionando" de "chave da
instituição num arquivo público": a chave injetada é a da integração da demo, o
arquivo em disco não contém chave nenhuma, e a resposta não pode ser cacheada
por proxy.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.api import ratelimit
from app.api.routers import demo as demo_router
from app.core.config import settings

CHAVE_DEMO = "chave-publica-da-demo"
CHAVE_AVA = "chave-privada-do-ava"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", f"ava:{CHAVE_AVA},demo:{CHAVE_DEMO}")
    monkeypatch.setattr(settings, "demo_enabled", True)
    monkeypatch.setattr(settings, "demo_consumidor", "demo")
    return TestClient(app_module.create_app(), raise_server_exceptions=False)


def test_demo_serve_o_html(client):
    resposta = client.get("/demo")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/html")
    assert "<title>Agente de Suporte Acadêmico</title>" in resposta.text


def test_demo_injeta_a_chave_da_integracao_da_demo(client):
    resposta = client.get("/demo")

    assert f'const API_KEY = "{CHAVE_DEMO}"' in resposta.text
    # A da instituição NUNCA pode sair aqui: é o modo de falha que a decisão de
    # usar uma integração própria existe para evitar.
    assert CHAVE_AVA not in resposta.text


def test_o_arquivo_em_disco_nao_contem_chave_nenhuma():
    """A chave é injetada em runtime; o que está versionado é o placeholder.

    Sem este teste, alguém "consertando" a demo colando a chave direto no HTML
    passaria despercebido — e ela iria para o git.
    """
    html = demo_router._INDEX.read_text(encoding="utf-8")

    assert demo_router._PLACEHOLDER in html
    assert CHAVE_DEMO not in html and CHAVE_AVA not in html


def test_a_chave_injetada_e_um_literal_js_escapado(client, monkeypatch):
    """`json.dumps`, não concatenação de aspas: uma chave com aspas quebraria o
    script (ou escaparia dele) se o placeholder fosse substituído no miolo."""
    monkeypatch.setattr(settings, "api_keys", 'demo:ab"cd')

    resposta = client.get("/demo")

    assert f"const API_KEY = {json.dumps('ab\"cd')}" in resposta.text


def test_demo_nao_pode_ser_cacheada(client):
    """A resposta tem uma chave dentro; um proxy guardando isso é como a chave
    sobrevive a uma rotação."""
    assert client.get("/demo").headers["cache-control"] == "no-store"


def test_sem_auth_a_chave_injetada_e_vazia(client, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)

    assert 'const API_KEY = ""' in client.get("/demo").text


def test_integracao_da_demo_ausente_nao_derruba_a_rota(client, monkeypatch):
    """Erro de configuração: a página carrega e o `fetch` toma 401 com
    explicação. A v1 (o AVA) não pode cair por causa disso."""
    monkeypatch.setattr(settings, "api_keys", f"ava:{CHAVE_AVA}")

    resposta = client.get("/demo")

    assert resposta.status_code == 200
    assert 'const API_KEY = ""' in resposta.text


def test_raiz_redireciona_para_a_demo(client):
    resposta = client.get("/", follow_redirects=False)

    # 307 e não 301: um permanente fica gravado no navegador e continuaria
    # redirecionando depois de a demo ser desligada em produção.
    assert (resposta.status_code, resposta.headers["location"]) == (307, "/demo")


def test_demo_desligada_tira_a_rota_do_ar(monkeypatch):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    monkeypatch.setattr(settings, "demo_enabled", False)
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)

    assert client.get("/demo").status_code == 404
    # E a v1 continua de pé — desligar a demo não é desligar o serviço.
    assert client.get("/v1/health").status_code == 200


# --- teto diário próprio da demo (o que torna a chave pública aceitável) ----


def test_teto_diario_por_consumidor_nao_afeta_os_outros():
    """A chave da demo vive num HTML público. Sem teto próprio, quem abrir o
    DevTools consome o orçamento do dia inteiro — inclusive o do AVA."""
    limiter = ratelimit.RateLimiter(por_minuto=100, teto_diario=1000, tetos_diarios={"demo": 2})

    limiter.verificar("demo")
    limiter.verificar("demo")
    with pytest.raises(ratelimit.RateLimitExcedido) as excecao:
        limiter.verificar("demo")

    # O AVA, sem teto próprio, segue respondendo ao global.
    limiter.verificar("ava")
    assert "desta integração" in str(excecao.value)
    # `Retry-After` da virada do dia, não dos 60s da janela por minuto.
    assert excecao.value.retry_after > 60


def test_teto_global_vence_o_teto_proprio_maior():
    """Nenhum consumidor passa do limite de custo do serviço por ter um teto
    próprio mais generoso — por isso o global é checado primeiro."""
    limiter = ratelimit.RateLimiter(por_minuto=100, teto_diario=1, tetos_diarios={"demo": 500})

    limiter.verificar("demo")
    with pytest.raises(ratelimit.RateLimitExcedido) as excecao:
        limiter.verificar("demo")

    assert "Teto diário do serviço" in str(excecao.value)


def test_consumidor_sem_teto_proprio_responde_so_ao_global():
    limiter = ratelimit.RateLimiter(por_minuto=100, teto_diario=5, tetos_diarios={"demo": 1})

    for _ in range(5):
        limiter.verificar("ava")

    with pytest.raises(ratelimit.RateLimitExcedido):
        limiter.verificar("ava")


# --- identidade visual servida da mesma origem ----------------------------


def test_logo_institucional_e_servido_em_static(client):
    """Mesma origem, não CDN: é o que mantém a promessa de a demo abrir numa
    máquina sem internet — e de a página não buscar nada em host de terceiro."""
    for arquivo in ("puc-campinas.png", "puc-brasao.png"):
        resposta = client.get(f"/static/{arquivo}")
        assert resposta.status_code == 200, arquivo
        assert resposta.headers["content-type"] == "image/png"


def test_html_so_referencia_imagens_de_mesma_origem(client):
    """Regressão da propriedade central da demo: nenhum `src`/`href` pode
    apontar para fora. Um dia alguém cola uma URL de CDN aqui sem perceber."""
    import re

    html = client.get("/demo").text
    externos = re.findall(r'(?:src|href)="((?:https?:)?//[^"]+)"', html)

    assert externos == []


def test_index_nao_e_alcancavel_por_static(client):
    """`/static` aponta para `app/static/img`, não para `app/static`. Servido
    direto, o index viria com o placeholder no lugar da chave — uma página
    quebrada sem explicação."""
    assert client.get("/static/index.html").status_code == 404
