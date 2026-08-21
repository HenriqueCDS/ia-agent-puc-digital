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
from app.api import deps
from app.core.models import Answer, RetrievedChunk


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
def client(monkeypatch):
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
    return TestClient(app, raise_server_exceptions=False)


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


# --- /v1/ask: os 3 valores de origem --------------------------------------


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
