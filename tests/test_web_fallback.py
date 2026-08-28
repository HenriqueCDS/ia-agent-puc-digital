"""Testes da busca externa restrita, sem rede e sem modelo de embedding real."""

import pytest

from app.agent import web_fallback
from app.core.config import settings
from app.core.models import Query


class FakeEmbeddings:
    """Dublê de Embeddings: similaridade decidida por marcação no texto.

    Snippet contendo "RELEVANTE" fica colinear com a pergunta (cosseno 1);
    qualquer outro fica ortogonal (cosseno 0). Evita carregar o modelo local
    de 1GB só para testar o corte por limiar.
    """

    def embed_query(self, texto):
        return [1.0, 0.0]

    def embed_documents(self, textos):
        return [[1.0, 0.0] if "RELEVANTE" in t else [0.0, 1.0] for t in textos]


@pytest.fixture(autouse=True)
def embeddings_falsas(monkeypatch):
    monkeypatch.setattr(web_fallback, "get_embeddings", FakeEmbeddings)


def _resultado(href, body="conteúdo RELEVANTE", title="titulo"):
    return {"href": href, "body": body, "title": title}


# --- allowlist ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://community.instructure.com/en/kb/articles/661210-how-do-i-submit",
        "https://www.puc-campinas.edu.br/calendario/",  # path curado do portal
        "https://www.puc-campinas.edu.br/biblioteca/servicos/",
        "https://pucdigital.puc-campinas.edu.br/pagina",  # subdomínio do programa: inteiro
    ],
)
def test_url_oficial_e_permitida(url):
    assert web_fallback.fonte_permitida(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        # fórum de usuários do Instructure: fora de /en/kb/, não é guia oficial
        "https://community.instructure.com/t5/Canvas-Question-Forum/x/td-p/1",
        "https://community.instructure.com/en/all-guides",
        # portal institucional fora dos paths curados (KB-2): vestibular,
        # avaliação institucional, notícia e landing page de campanha
        "https://www.puc-campinas.edu.br/graduacao/direito/",
        "https://vestibular.puc-campinas.edu.br/curso/",
        "https://www.puc-campinas.edu.br/avaliacao-institucional/avaliacao-do-ensino/",
        "https://www.puc-campinas.edu.br/termo-aceite-dados-pessoais-lp/",
        # domínio sósia: termina com o host permitido, mas não é ele
        "https://puc-campinas.edu.br.attacker.com/pagina",
        "https://blog.exemplo.com/canvas",
        "javascript:alert(1)",
    ],
)
def test_url_fora_da_allowlist_e_recusada(url):
    assert web_fallback.fonte_permitida(url) is None


def test_redirect_do_duckduckgo_e_resolvido_antes_de_validar():
    envolvida = (
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.puc-campinas.edu.br%2Fcalendario%2F"
    )
    assert web_fallback.fonte_permitida(envolvida) is not None


def test_resultado_fora_da_allowlist_nao_vira_contexto(monkeypatch):
    """A validação por URL não confia no operador `site:` do DuckDuckGo."""
    monkeypatch.setattr(
        web_fallback,
        "_buscar_em",
        lambda fonte, pergunta: [_resultado("https://spam.example.com/x")],
    )

    assert web_fallback.buscar_na_web(Query(text="como envio atividade?")) == []


# --- relevância e blocklist --------------------------------------------------


def test_snippet_irrelevante_e_cortado_pelo_limiar(monkeypatch):
    monkeypatch.setattr(
        web_fallback,
        "_buscar_em",
        lambda fonte, pergunta: [
            _resultado("https://www.puc-campinas.edu.br/calendario/a", body="texto qualquer")
        ],
    )

    assert web_fallback.buscar_na_web(Query(text="como envio atividade?")) == []


def test_resultado_relevante_vira_chunk_citando_a_url(monkeypatch):
    url = "https://community.instructure.com/en/kb/articles/661210-submit"
    monkeypatch.setattr(web_fallback, "_buscar_em", lambda f, p: [_resultado(url)])

    chunks = web_fallback.buscar_na_web(Query(text="como envio atividade?"))

    assert len(chunks) == 1
    assert chunks[0].citation == url  # a citação da web é a própria URL
    assert chunks[0].document.metadata["origem"] == "web"


def test_urls_repetidas_entre_fontes_sao_deduplicadas(monkeypatch):
    url = "https://www.puc-campinas.edu.br/calendario/a"
    monkeypatch.setattr(web_fallback, "_buscar_em", lambda f, p: [_resultado(url)])

    chunks = web_fallback.buscar_na_web(Query(text="pergunta"))

    assert len(chunks) == 1  # todas as fontes da allowlist devolvendo o mesmo link


def test_assunto_sensivel_nao_chega_a_buscar(monkeypatch):
    def nao_deveria_ser_chamado(fonte, pergunta):
        raise AssertionError("busca externa não pode rodar para assunto sensível")

    monkeypatch.setattr(web_fallback, "_buscar_em", nao_deveria_ser_chamado)

    assert web_fallback.buscar_na_web(Query(text="Como faço a rematrícula?")) == []
    assert web_fallback.buscar_na_web(Query(text="Não recebi meu boleto")) == []


def test_falha_da_busca_vira_lista_vazia_e_nao_excecao(monkeypatch):
    """Rate limit ou mudança no HTML do DuckDuckGo não pode virar erro pro usuário."""
    from ddgs import DDGS
    from ddgs.exceptions import RatelimitException

    def ddgs_quebrado(self, *args, **kwargs):
        raise RatelimitException("429")

    monkeypatch.setattr(DDGS, "text", ddgs_quebrado)

    assert web_fallback.buscar_na_web(Query(text="pergunta qualquer")) == []


# --- roteamento por assunto --------------------------------------------------


def test_assunto_limita_as_fontes_consultadas():
    canvas = web_fallback._fontes_para("canvas")
    assert canvas and all(f.host == "community.instructure.com" for f in canvas)

    puc = web_fallback._fontes_para("puc-digital")
    assert puc and all("puc-campinas" in f.host for f in puc)


def test_sem_assunto_consulta_todas_as_fontes():
    assert web_fallback._fontes_para(None) == web_fallback.WEB_ALLOWLIST


def test_assunto_sem_resultado_amplia_para_allowlist_inteira(monkeypatch):
    """A rodada restrita ao assunto (canvas -> só community.instructure.com) não
    acha nada; a resposta está em puc-campinas.edu.br, fora do assunto detectado
    — uma 2a rodada com a allowlist inteira deve achar."""

    def buscar(fonte, pergunta):
        if fonte.host == "puc-campinas.edu.br":
            return [_resultado("https://www.puc-campinas.edu.br/calendario/pagina")]
        return []

    monkeypatch.setattr(web_fallback, "_buscar_em", buscar)

    chunks = web_fallback.buscar_na_web(Query(text="como envio atividade?", assunto="canvas"))

    assert len(chunks) == 1
    assert chunks[0].citation == "https://www.puc-campinas.edu.br/calendario/pagina"


def test_assunto_com_resultado_nao_amplia_a_busca(monkeypatch):
    """Achou algo na 1a rodada: não tenta as demais fontes da allowlist."""
    chamadas = []

    def buscar(fonte, pergunta):
        chamadas.append(fonte.host)
        return [_resultado("https://community.instructure.com/en/kb/articles/1-x")]

    monkeypatch.setattr(web_fallback, "_buscar_em", buscar)

    web_fallback.buscar_na_web(Query(text="como envio atividade?", assunto="canvas"))

    assert chamadas == ["community.instructure.com"]


def test_no_maximo_web_max_chunks_chegam_ao_llm(monkeypatch):
    muitos = [
        _resultado(f"https://www.puc-campinas.edu.br/calendario/pagina-{i}")
        for i in range(settings.web_max_chunks + 3)
    ]
    monkeypatch.setattr(web_fallback, "_buscar_em", lambda f, p: muitos)

    chunks = web_fallback.buscar_na_web(Query(text="pergunta"))

    assert len(chunks) == settings.web_max_chunks
