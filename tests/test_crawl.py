"""Testes do crawler da allowlist (scripts/crawl.py) — sem rede, sem banco."""

from types import SimpleNamespace

import pytest

from app.core.config import FonteWeb
from scripts import crawl


def _resp(*, ok=True, content=b"", text="", content_type="text/html"):
    return SimpleNamespace(
        ok=ok,
        content=content,
        text=text or content.decode("utf-8", "ignore"),
        headers={"content-type": content_type},
    )


class FakeSessao:
    """Session que devolve respostas de um dict {url: resposta}."""

    def __init__(self, rotas):
        self.rotas = rotas
        self.pedidos = []

    def get(self, url, timeout=None):
        self.pedidos.append(url)
        if url not in self.rotas:
            return _resp(ok=False)
        return self.rotas[url]


_FONTE = FonteWeb(
    host="puc-campinas.edu.br",
    path_prefixes=("/calendario/", "/biblioteca/"),
    assunto="puc-digital",
)

_SITEMAP = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.puc-campinas.edu.br/calendario/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/biblioteca/servicos/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/vestibular/inscricao/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/calendario/2026#rodape</loc></url>
</urlset>"""


def test_locs_urlset():
    locs, eh_indice = crawl._locs(_SITEMAP)
    assert not eh_indice
    assert "https://www.puc-campinas.edu.br/calendario/" in locs


def test_locs_sitemapindex_e_detectado():
    xml = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x/sub-sitemap.xml</loc></sitemap>
    </sitemapindex>"""
    locs, eh_indice = crawl._locs(xml)
    assert eh_indice and locs == ["https://x/sub-sitemap.xml"]


def test_descobrir_urls_filtra_pelos_path_prefixes():
    sessao = FakeSessao(
        {"https://puc-campinas.edu.br/sitemap_index.xml": _resp(content=_SITEMAP, content_type="application/xml")}
    )

    urls = crawl.descobrir_urls(sessao, _FONTE)

    assert urls == [
        "https://www.puc-campinas.edu.br/biblioteca/servicos/",
        "https://www.puc-campinas.edu.br/calendario/",
        "https://www.puc-campinas.edu.br/calendario/2026",  # fragmento removido
    ]
    assert not any("vestibular" in u for u in urls)  # fora dos path_prefixes


def test_descobrir_urls_segue_o_sitemap_index():
    index = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://puc-campinas.edu.br/sitemap-paginas.xml</loc></sitemap>
    </sitemapindex>"""
    sessao = FakeSessao(
        {
            "https://puc-campinas.edu.br/sitemap_index.xml": _resp(content=index, content_type="text/xml"),
            "https://puc-campinas.edu.br/sitemap-paginas.xml": _resp(content=_SITEMAP, content_type="text/xml"),
        }
    )

    urls = crawl.descobrir_urls(sessao, _FONTE)
    assert "https://www.puc-campinas.edu.br/calendario/" in urls


def test_extrair_tira_menu_e_rodape():
    html = """
    <html><head><title>Calendário Acadêmico</title></head>
    <body>
      <nav>Menu Início Cursos Contato</nav>
      <main><p>As provas do 1º bimestre ocorrem entre 10 e 20 de março.</p></main>
      <footer>PUC-Campinas 2026 - todos os direitos reservados</footer>
    </body></html>
    """
    titulo, texto = crawl._extrair(html)
    assert titulo == "Calendário Acadêmico"
    assert "provas do 1º bimestre" in texto
    assert "todos os direitos reservados" not in texto
    assert "Menu Início" not in texto


def test_documento_marca_origem_web_mas_mantem_assunto():
    doc = crawl._documento(
        "https://www.puc-campinas.edu.br/calendario/", "Calendário", "corpo da página", _FONTE
    )
    assert doc.metadata["assunto"] == "puc-digital"  # NÃO "web" — filtro do retrieval
    assert doc.metadata["categoria"] == "web"
    assert doc.metadata["source_type"] == "web"
    assert doc.metadata["source_path"] == "https://www.puc-campinas.edu.br/calendario/"
    assert doc.metadata["source_name"] == "https://www.puc-campinas.edu.br/calendario/"


def test_crawl_fonte_dry_run_nao_indexa(monkeypatch):
    monkeypatch.setattr(
        crawl, "descobrir_urls", lambda s, f: ["https://www.puc-campinas.edu.br/calendario/"]
    )

    def nao_chamar(*a, **k):
        raise AssertionError("dry-run não pode indexar")

    monkeypatch.setattr(crawl, "ingest_documents", nao_chamar)

    stats = crawl._crawl_fonte(
        _FONTE, sessao=FakeSessao({}), limite=0, delay=0, dry_run=True
    )
    assert stats["urls"] == 1 and stats["paginas"] == 0
