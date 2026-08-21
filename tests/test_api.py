"""Testes da borda HTTP (app/api/). Sem banco, sem Gemini: `answer` e a lista de
assuntos válidos são dublados — o núcleo já está coberto por test_responder.py.

`create_app()` liga telemetria (`telemetry_store.habilitar()`), que muta um
global de processo (`telemetry._persistir`) para apontar para o Postgres. A
fixture dubla `habilitar` antes de criar o app: sem isso, rodar este arquivo
"vazaria" persistência real de telemetria para o resto da suíte de testes.

`TestClient(app)` sem `with` (context manager) não dispara `lifespan` — por
isso os testes não pagam o warm-up (nem batem no banco/modelo real) mesmo
sem dublar `_lifespan` diretamente.
"""

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from app.agent import responder
from app.api import app as app_module
from app.api import deps, ratelimit
from app.core.config import settings
from app.core.models import Answer, RetrievedChunk

CHAVE = "chave-de-teste-do-ava"


def _chunk_base(score=0.9, page=2):
    return RetrievedChunk(
        document=Document(page_content="x", metadata={"source_name": "guia.pdf", "page": page}),
        score=score,
    )


def _chunk_web(score=0.6, url="https://community.instructure.com/en/kb/articles/1"):
    return RetrievedChunk(
        document=Document(
            page_content="x",
            metadata={"source_name": url, "origem": "web", "titulo": "Submitting an assignment"},
        ),
        score=score,
    )


@pytest.fixture
def config_auth(monkeypatch):
    """Uma integração cadastrada ("ava"). Os testes das outras rotas mandam a
    chave por default (ver `client`); os de autenticação a omitem de propósito."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", f"ava:{CHAVE}")
    return CHAVE


@pytest.fixture
def app_sem_auth_config(monkeypatch, config_auth):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()
    app.dependency_overrides[deps.get_assuntos_validos] = lambda: ["canvas", "puc-digital"]
    return app


@pytest.fixture
def client(monkeypatch, config_auth):
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()
    app.dependency_overrides[deps.get_assuntos_validos] = lambda: ["canvas", "puc-digital"]
    # `raise_server_exceptions=False`: o `ServerErrorMiddleware` do Starlette
    # SEMPRE relança a exceção original depois de gerar a resposta 500 (é
    # assim que um servidor ASGI de verdade loga o erro) — o default do
    # TestClient propaga isso como exceção Python no teste. Sem isto,
    # `test_erro_nao_mapeado_da_500_sem_vazar_stack` explodiria mesmo com o
    # handler funcionando certo. `False` reflete o que um cliente HTTP real
    # vê: só a resposta.
    # A chave vai em todas as requests deste cliente: os testes abaixo são
    # sobre o contrato das rotas, não sobre autenticação (que tem bloco
    # próprio no fim do arquivo, com um cliente sem header).
    return TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": CHAVE})


def _dublar_answer(monkeypatch, resultado: Answer) -> None:
    from app.api.routers import v1

    monkeypatch.setattr(v1, "answer", lambda query: resultado)


def _dublar_answer_direta(monkeypatch, fake_answer) -> None:
    from app.api.routers import v1

    monkeypatch.setattr(v1, "answer", fake_answer)


# --- health / assuntos ---------------------------------------------------


def test_health(client):
    resposta = client.get("/v1/health")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}


def test_assuntos_devolve_a_lista_dublada(client):
    resposta = client.get("/v1/assuntos")

    assert resposta.status_code == 200
    assert resposta.json() == {"assuntos": ["canvas", "puc-digital"]}


# --- /v1/ask: os 4 valores de origem --------------------------------------


def test_ask_origem_base(client, monkeypatch):
    _dublar_answer(
        monkeypatch,
        Answer(text="Resposta.", sources=[_chunk_base()], grounded=True, origem="base"),
    )

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["origem"] == "base"
    assert corpo["encontrou_na_base"] is True
    assert corpo["fontes"] == [
        {"titulo": "guia.pdf", "tipo": "documento", "url": None, "pagina": 3, "score": 0.9}
    ]
    assert corpo["request_id"]  # presente e não vazio
    assert resposta.headers["X-Request-Id"] == corpo["request_id"]


def test_ask_origem_web_traz_url_clicavel(client, monkeypatch):
    """T1.3: a URL da fonte web não pode mais ser jogada fora."""
    _dublar_answer(
        monkeypatch,
        Answer(text="Resposta da web.", sources=[_chunk_web()], grounded=False, origem="web"),
    )

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    corpo = resposta.json()
    assert corpo["origem"] == "web"
    assert corpo["encontrou_na_base"] is False
    (fonte,) = corpo["fontes"]
    assert fonte["tipo"] == "web"
    assert fonte["titulo"] == "Submitting an assignment"
    assert fonte["url"] == "https://community.instructure.com/en/kb/articles/1"
    assert fonte["pagina"] is None


def test_ask_origem_nenhuma(client, monkeypatch):
    _dublar_answer(
        monkeypatch, Answer(text="Não encontrei.", sources=[], grounded=False, origem="nenhuma")
    )

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    corpo = resposta.json()
    assert corpo["origem"] == "nenhuma"
    assert corpo["fontes"] == []


def test_ask_origem_encaminhado(client, monkeypatch):
    """Regressão: `origem="encaminhado"` entrou no agente sem entrar no
    `Literal` do schema, e a resposta virou literal_error na serialização —
    erro 500 numa pergunta que o agente tinha respondido certo."""
    _dublar_answer(
        monkeypatch,
        Answer(
            text="Sobre esse tipo de assunto, recomendo verificar diretamente...",
            sources=[],
            grounded=False,
            origem="encaminhado",
        ),
    )

    resposta = client.post("/v1/ask", json={"pergunta": "qual o meu boleto desse mes?"})

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["origem"] == "encaminhado"
    assert corpo["encontrou_na_base"] is False
    assert corpo["fontes"] == []


def test_todo_valor_de_origem_do_agente_e_aceito_pelo_schema(client, monkeypatch):
    """A garantia estrutural: percorre os valores declarados em `Origem` (o
    tipo que o agente usa) e prova que a API serializa todos. É o teste que
    torna impossível a divergência entre o núcleo e o contrato HTTP."""
    from typing import get_args

    from app.core.models import Origem

    for origem in get_args(Origem):
        _dublar_answer(
            monkeypatch,
            Answer(text="Resposta.", sources=[], grounded=False, origem=origem),
        )

        resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

        assert resposta.status_code == 200, f"origem={origem} rejeitada pelo schema"
        assert resposta.json()["origem"] == origem


# --- validação e erros (T1.5, T1.6, T1.7) ---------------------------------


def test_pergunta_curta_demais_da_422(client):
    resposta = client.post("/v1/ask", json={"pergunta": "oi"})

    assert resposta.status_code == 422
    assert resposta.json()["erro"] == "validacao"


def test_pergunta_longa_demais_da_422(client):
    resposta = client.post("/v1/ask", json={"pergunta": "x" * 1001})

    assert resposta.status_code == 422
    assert resposta.json()["erro"] == "validacao"


def test_pergunta_vazia_apos_strip_da_422(client, monkeypatch):
    """3+ chars passam o Pydantic, mas normalize() rejeita só espaço."""

    def normaliza_e_falha(query):
        raise ValueError("Pergunta vazia.")

    monkeypatch.setattr(responder, "normalize", normaliza_e_falha)
    from app.api.routers import v1

    monkeypatch.setattr(v1, "answer", responder.answer)

    resposta = client.post("/v1/ask", json={"pergunta": "   "})

    assert resposta.status_code == 422
    assert resposta.json()["erro"] == "pergunta_invalida"


def test_assunto_invalido_da_422_sem_chamar_answer(client, monkeypatch):
    """T1.7: hoje isso cairia silenciosamente no fallback web com zero chunks."""
    chamado = []
    from app.api.routers import v1

    monkeypatch.setattr(v1, "answer", lambda query: chamado.append(query) or None)

    resposta = client.post("/v1/ask", json={"pergunta": "como envio?", "assunto": "Canvas"})

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["erro"] == "assunto_invalido"
    assert "canvas" in corpo["detalhe"] and "puc-digital" in corpo["detalhe"]
    assert chamado == []  # nunca chegou a chamar answer()


def test_anexo_nao_suportado_da_501(client, monkeypatch):
    def levanta(query):
        raise NotImplementedError("Interpretação de imagens ainda não faz parte do escopo (v1).")

    _dublar_answer_direta(monkeypatch, levanta)

    resposta = client.post("/v1/ask", json={"pergunta": "o que tem nesse print?"})

    assert resposta.status_code == 501
    assert resposta.json()["erro"] == "recurso_nao_suportado"


def test_google_api_key_ausente_da_503(client, monkeypatch):
    def levanta(query):
        raise RuntimeError("GOOGLE_API_KEY não configurada.")

    _dublar_answer_direta(monkeypatch, levanta)

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "servico_indisponivel"


def test_banco_fora_do_ar_da_503(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def levanta(query):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    _dublar_answer_direta(monkeypatch, levanta)

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "banco_indisponivel"


def test_erro_nao_mapeado_da_500_sem_vazar_stack(client, monkeypatch):
    def levanta(query):
        raise ZeroDivisionError("detalhe interno que não deve vazar")

    _dublar_answer_direta(monkeypatch, levanta)

    resposta = client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    assert resposta.status_code == 500
    corpo = resposta.json()
    assert corpo["erro"] == "erro_interno"
    assert "detalhe interno" not in corpo["detalhe"]  # nada de stack/mensagem crua


# --- T2.1: autenticação por API key ---------------------------------------


def _client_cru(app) -> TestClient:
    """Sem a chave nos headers default — cada teste decide o que mandar."""
    return TestClient(app, raise_server_exceptions=False)


def test_ask_sem_chave_da_401(app_sem_auth_config):
    resposta = _client_cru(app_sem_auth_config).post(
        "/v1/ask", json={"pergunta": "como envio atividade?"}
    )

    assert resposta.status_code == 401
    assert resposta.json()["erro"] == "nao_autenticado"


def test_ask_com_chave_invalida_da_401(app_sem_auth_config):
    resposta = _client_cru(app_sem_auth_config).post(
        "/v1/ask",
        json={"pergunta": "como envio atividade?"},
        headers={"X-API-Key": "chave-que-nao-existe"},
    )

    assert resposta.status_code == 401
    corpo = resposta.json()
    assert corpo["erro"] == "nao_autenticado"
    # Não pode virar oráculo de enumeração: a mensagem não distingue "chave
    # inexistente" de "chave existente com valor errado", e jamais ecoa a chave.
    assert "chave-que-nao-existe" not in corpo["detalhe"]


def test_assuntos_sem_chave_da_401_sem_tocar_no_banco(app_sem_auth_config):
    """A autenticação vem ANTES de `get_assuntos_validos` na assinatura da
    rota — sem chave, a request não deve nem abrir conexão."""
    chamado = []
    app_sem_auth_config.dependency_overrides[deps.get_assuntos_validos] = (
        lambda: chamado.append(1) or []
    )

    resposta = _client_cru(app_sem_auth_config).get("/v1/assuntos")

    assert resposta.status_code == 401
    assert chamado == []


def test_health_continua_publico(app_sem_auth_config):
    assert _client_cru(app_sem_auth_config).get("/v1/health").status_code == 200


def test_auth_desligada_libera_sem_chave(monkeypatch):
    """Kill switch de desenvolvimento local (`API_AUTH_ENABLED=false`)."""
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()
    app.dependency_overrides[deps.get_assuntos_validos] = lambda: ["canvas"]

    assert _client_cru(app).get("/v1/assuntos").status_code == 200


def test_auth_ligada_sem_chaves_cadastradas_da_503(monkeypatch):
    """Fail-closed: erro de configuração derruba a rota, nunca a abre."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", "")
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()
    app.dependency_overrides[deps.get_assuntos_validos] = lambda: ["canvas"]

    resposta = _client_cru(app).get("/v1/assuntos")

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "servico_indisponivel"


def test_consumidor_vai_para_a_telemetria_como_canal(client, monkeypatch):
    """Metade da razão de T2.1: sem identificar quem chamou, não há como
    atribuir o custo do Gemini por canal."""
    canais = []
    from app.api.routers import v1

    monkeypatch.setattr(v1.telemetry, "set_canal", canais.append)
    _dublar_answer(monkeypatch, Answer(text="ok", sources=[], grounded=True, origem="base"))

    client.post("/v1/ask", json={"pergunta": "como envio atividade?"})

    assert canais == ["api:ava"]


# --- T2.2: CORS ------------------------------------------------------------


def test_cors_libera_origem_declarada(monkeypatch, config_auth):
    monkeypatch.setattr(settings, "cors_origins", "https://ava.puc-campinas.edu.br")
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()

    resposta = _client_cru(app).get(
        "/v1/health", headers={"Origin": "https://ava.puc-campinas.edu.br"}
    )

    assert resposta.headers["access-control-allow-origin"] == "https://ava.puc-campinas.edu.br"


def test_cors_bloqueia_origem_nao_listada(monkeypatch, config_auth):
    monkeypatch.setattr(settings, "cors_origins", "https://ava.puc-campinas.edu.br")
    monkeypatch.setattr(app_module.telemetry_store, "habilitar", lambda: None)
    app = app_module.create_app()

    resposta = _client_cru(app).get("/v1/health", headers={"Origin": "https://site-qualquer.com"})

    # Sem o cabeçalho, o navegador é quem recusa a resposta ao JS chamador.
    assert "access-control-allow-origin" not in resposta.headers


def test_asterisco_em_cors_origins_e_ignorado():
    """`*` junto com auth por header significa "qualquer página pode gastar a
    chave de quem a estiver usando" — descartado na leitura da config."""
    from app.core.config import Settings

    assert Settings(cors_origins="*,https://ok.edu.br").cors_origins_lista == ["https://ok.edu.br"]


# --- T2.3: rate limit ------------------------------------------------------


def test_estourar_o_limite_por_minuto_da_429_com_retry_after(client, monkeypatch):
    monkeypatch.setattr(ratelimit.get_rate_limiter(), "por_minuto", 2)
    _dublar_answer(monkeypatch, Answer(text="ok", sources=[], grounded=True, origem="base"))
    payload = {"pergunta": "como envio atividade?"}

    assert client.post("/v1/ask", json=payload).status_code == 200
    assert client.post("/v1/ask", json=payload).status_code == 200
    resposta = client.post("/v1/ask", json=payload)

    assert resposta.status_code == 429
    assert resposta.json()["erro"] == "rate_limit"
    assert 0 < int(resposta.headers["Retry-After"]) <= 61


def test_teto_diario_global_da_429_ate_a_virada_do_dia(client, monkeypatch):
    limiter = ratelimit.get_rate_limiter()
    monkeypatch.setattr(limiter, "teto_diario", 1)
    _dublar_answer(monkeypatch, Answer(text="ok", sources=[], grounded=True, origem="base"))
    payload = {"pergunta": "como envio atividade?"}

    assert client.post("/v1/ask", json=payload).status_code == 200
    resposta = client.post("/v1/ask", json=payload)

    assert resposta.status_code == 429
    assert "diário" in resposta.json()["detalhe"]
    # Esperar 60s não adianta quando o que estourou é o orçamento do dia.
    assert int(resposta.headers["Retry-After"]) > 60


def test_assuntos_nao_consome_o_orcamento_das_perguntas(client, monkeypatch):
    """`/assuntos` é autenticada mas fora do rate limit: a demo o chama a cada
    carga de página, e contá-la no mesmo balde gastaria a cota do `/ask`."""
    monkeypatch.setattr(ratelimit.get_rate_limiter(), "por_minuto", 1)

    for _ in range(5):
        assert client.get("/v1/assuntos").status_code == 200


def test_rate_limit_e_por_consumidor(monkeypatch, app_sem_auth_config):
    """Duas integrações não dividem o mesmo balde — senão a mais movimentada
    derruba a outra."""
    monkeypatch.setattr(settings, "api_keys", f"ava:{CHAVE},portal:chave-do-portal")
    monkeypatch.setattr(ratelimit.get_rate_limiter(), "por_minuto", 1)
    _dublar_answer(monkeypatch, Answer(text="ok", sources=[], grounded=True, origem="base"))
    cliente = _client_cru(app_sem_auth_config)
    payload = {"pergunta": "como envio atividade?"}

    assert cliente.post("/v1/ask", json=payload, headers={"X-API-Key": CHAVE}).status_code == 200
    assert cliente.post("/v1/ask", json=payload, headers={"X-API-Key": CHAVE}).status_code == 429
    outra = cliente.post("/v1/ask", json=payload, headers={"X-API-Key": "chave-do-portal"})
    assert outra.status_code == 200


# --- T2.6: /ready ----------------------------------------------------------


def _dublar_banco(monkeypatch, ok: bool) -> None:
    from contextlib import contextmanager

    from app.api.routers import v1

    class _Sessao:
        def execute(self, _):
            if not ok:
                raise OSError("connection refused")

    @contextmanager
    def _maker():
        yield _Sessao()

    class _Store:
        session_maker = staticmethod(_maker)

    monkeypatch.setattr(v1, "get_vector_store", lambda: _Store())


def test_ready_ok_com_banco_e_chave(client, monkeypatch):
    _dublar_banco(monkeypatch, ok=True)
    monkeypatch.setattr(settings, "google_api_key", "chave-qualquer")

    resposta = client.get("/v1/ready")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok", "checagens": {"banco": True, "chave_llm": True}}


def test_ready_da_503_com_banco_fora_do_ar(client, monkeypatch):
    """O que T2.6 existe para consertar: até aqui `/health` respondia "ok"
    incondicionalmente e o monitoramento ficava verde com o banco derrubado."""
    _dublar_banco(monkeypatch, ok=False)
    monkeypatch.setattr(settings, "google_api_key", "chave-qualquer")

    resposta = client.get("/v1/ready")

    assert resposta.status_code == 503
    corpo = resposta.json()
    assert corpo["status"] == "indisponivel"
    assert corpo["checagens"] == {"banco": False, "chave_llm": True}


def test_ready_da_503_sem_chave_do_llm(client, monkeypatch):
    _dublar_banco(monkeypatch, ok=True)
    monkeypatch.setattr(settings, "google_api_key", "")

    resposta = client.get("/v1/ready")

    assert resposta.status_code == 503
    assert resposta.json()["checagens"]["chave_llm"] is False


def test_health_nao_checa_dependencia(client, monkeypatch):
    """Liveness: com o banco fora, `/health` continua 200 de propósito —
    reiniciar a app não conserta o Postgres."""
    _dublar_banco(monkeypatch, ok=False)

    assert client.get("/v1/health").status_code == 200
