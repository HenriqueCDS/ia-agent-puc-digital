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


# path_prefixes precisam ser um subconjunto do que a WEB_ALLOWLIST real permite:
# `descobrir_urls` revalida cada URL do sitemap com `fonte_permitida`, que casa
# contra a allowlist de produção, não contra esta `_FONTE`.
_FONTE = FonteWeb(
    host="puc-campinas.edu.br",
    path_prefixes=("/mestrado-e-doutorado/", "/biblioteca/"),
    assunto="puc-digital",
)

_SITEMAP = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.puc-campinas.edu.br/mestrado-e-doutorado/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/biblioteca/servicos/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/vestibular/inscricao/</loc></url>
  <url><loc>https://www.puc-campinas.edu.br/mestrado-e-doutorado/2026#rodape</loc></url>
</urlset>"""


def test_locs_urlset():
    locs, eh_indice = crawl._locs(_SITEMAP)
    assert not eh_indice
    assert "https://www.puc-campinas.edu.br/mestrado-e-doutorado/" in locs


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

    urls, confiavel = crawl.descobrir_urls(sessao, _FONTE)

    assert urls == [
        "https://www.puc-campinas.edu.br/biblioteca/servicos/",
        "https://www.puc-campinas.edu.br/mestrado-e-doutorado/",
        "https://www.puc-campinas.edu.br/mestrado-e-doutorado/2026",  # fragmento removido
    ]
    assert not any("vestibular" in u for u in urls)  # fora dos path_prefixes
    assert confiavel is True  # o sitemap respondeu e não era índice com sub-sitemap faltando


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

    urls, confiavel = crawl.descobrir_urls(sessao, _FONTE)
    assert "https://www.puc-campinas.edu.br/mestrado-e-doutorado/" in urls
    assert confiavel is True


def test_descobrir_urls_nao_confiavel_quando_sub_sitemap_falha():
    """Índice aponta para 2 sub-sitemaps e um deles dá 500: faltam URLs, então
    `confiavel` é False e o prune não pode rodar (KB-5)."""
    index = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://puc-campinas.edu.br/sitemap-a.xml</loc></sitemap>
      <sitemap><loc>https://puc-campinas.edu.br/sitemap-b.xml</loc></sitemap>
    </sitemapindex>"""
    sessao = FakeSessao(
        {
            "https://puc-campinas.edu.br/sitemap_index.xml": _resp(content=index, content_type="text/xml"),
            "https://puc-campinas.edu.br/sitemap-a.xml": _resp(content=_SITEMAP, content_type="text/xml"),
            # sitemap-b.xml ausente do dict -> FakeSessao devolve ok=False
        }
    )

    urls, confiavel = crawl.descobrir_urls(sessao, _FONTE)
    assert urls  # o sub-sitemap que respondeu ainda trouxe URLs
    assert confiavel is False


def test_descobrir_urls_confiavel_ignora_404_dos_candidatos_iniciais():
    """Dos 6 caminhos de sitemap chutados, só 1 responde — isso é o normal, não
    conta como falha."""
    sessao = FakeSessao(
        {"https://www.puc-campinas.edu.br/sitemap.xml": _resp(content=_SITEMAP, content_type="text/xml")}
    )

    urls, confiavel = crawl.descobrir_urls(sessao, _FONTE)
    assert urls and confiavel is True


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
        "https://www.puc-campinas.edu.br/mestrado-e-doutorado/", "Calendário", "corpo da página", _FONTE
    )
    assert doc.metadata["assunto"] == "puc-digital"  # NÃO "web" — filtro do retrieval
    assert doc.metadata["categoria"] == "web"
    assert doc.metadata["source_type"] == "web"
    assert doc.metadata["source_path"] == "https://www.puc-campinas.edu.br/mestrado-e-doutorado/"
    assert doc.metadata["source_name"] == "https://www.puc-campinas.edu.br/mestrado-e-doutorado/"


def test_crawl_fonte_dry_run_nao_indexa(monkeypatch):
    monkeypatch.setattr(
        crawl,
        "descobrir_urls",
        lambda s, f: (["https://www.puc-campinas.edu.br/mestrado-e-doutorado/"], True),
    )

    def nao_chamar(*a, **k):
        raise AssertionError("dry-run não pode indexar")

    monkeypatch.setattr(crawl, "ingest_documents", nao_chamar)

    stats = crawl._crawl_fonte(
        _FONTE, sessao=FakeSessao({}), limite=0, delay=0, dry_run=True
    )
    assert stats["urls"] == 1 and stats["paginas"] == 0


# --- prune de páginas fora do sitemap (KB-5) ---------------------------------

_ALLOW = "https://www.puc-campinas.edu.br/biblioteca"  # bate na WEB_ALLOWLIST real


def test_podar_orfas_remove_so_a_pagina_fora_do_sitemap(monkeypatch):
    monkeypatch.setattr(crawl, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        crawl,
        "list_web_sources",
        lambda store: [(f"{_ALLOW}/viva/", 2), (f"{_ALLOW}/removida/", 3)],
    )
    apagadas = []
    monkeypatch.setattr(crawl, "delete_by_source", lambda store, sp: apagadas.append(sp) or 3)

    stats = crawl._podar_orfas(_FONTE, {f"{_ALLOW}/viva/"}, dry_run=False, forcar=False)

    assert apagadas == [f"{_ALLOW}/removida/"]
    assert stats == {"orfas": 1, "chunks_podados": 3}


def test_podar_orfas_dry_run_lista_mas_nao_apaga(monkeypatch):
    monkeypatch.setattr(crawl, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        crawl,
        "list_web_sources",
        lambda store: [(f"{_ALLOW}/viva/", 2), (f"{_ALLOW}/removida/", 3)],
    )
    monkeypatch.setattr(
        crawl, "delete_by_source", lambda *a: pytest.fail("dry-run não pode apagar")
    )

    stats = crawl._podar_orfas(_FONTE, {f"{_ALLOW}/viva/"}, dry_run=True, forcar=False)
    assert stats == {"orfas": 1, "chunks_podados": 0}


def test_podar_orfas_aborta_quando_maioria_sumiu_e_respeita_forcar(monkeypatch):
    monkeypatch.setattr(crawl, "get_vector_store", lambda: object())
    monkeypatch.setattr(
        crawl,
        "list_web_sources",
        lambda store: [(f"{_ALLOW}/a/", 1), (f"{_ALLOW}/b/", 1), (f"{_ALLOW}/c/", 1)],
    )
    apagadas = []
    monkeypatch.setattr(crawl, "delete_by_source", lambda store, sp: apagadas.append(sp) or 1)

    # 2 de 3 fora do sitemap (67% > _PRUNE_FRACAO_MAX): não apaga sem --prune-force
    stats = crawl._podar_orfas(_FONTE, {f"{_ALLOW}/a/"}, dry_run=False, forcar=False)
    assert apagadas == [] and stats["chunks_podados"] == 0

    stats_forcado = crawl._podar_orfas(_FONTE, {f"{_ALLOW}/a/"}, dry_run=False, forcar=True)
    assert sorted(apagadas) == [f"{_ALLOW}/b/", f"{_ALLOW}/c/"]
    assert stats_forcado["chunks_podados"] == 2


def test_crawl_fonte_pula_pagina_gigante(monkeypatch):
    """Página acima de `_MAX_HTML_BYTES` (visualizador de PDF embutido, DOM
    gigante) é pulada antes do BeautifulSoup/chunk/embed — é o que estourava a
    RAM em ambiente pequeno."""
    url = "https://www.puc-campinas.edu.br/biblioteca/manual/"
    monkeypatch.setattr(crawl, "descobrir_urls", lambda s, f: ([url], True))
    monkeypatch.setattr(
        crawl, "ingest_documents", lambda *a, **k: pytest.fail("não deve indexar página gigante")
    )
    sessao = FakeSessao(
        {url: _resp(content=b"x" * (crawl._MAX_HTML_BYTES + 1), text="<html></html>")}
    )

    stats = crawl._crawl_fonte(_FONTE, sessao=sessao, limite=0, delay=0, dry_run=False)

    assert stats["pulados"] >= 1 and stats["paginas"] == 0


def test_crawl_fonte_nao_poda_quando_sitemap_nao_confiavel(monkeypatch):
    monkeypatch.setattr(crawl, "descobrir_urls", lambda s, f: ([f"{_ALLOW}/x/"], False))
    monkeypatch.setattr(crawl, "ingest_documents", lambda *a, **k: (0, 0))
    monkeypatch.setattr(
        crawl, "_podar_orfas", lambda *a, **k: pytest.fail("não pode podar sem sitemap confiável")
    )

    stats = crawl._crawl_fonte(
        _FONTE, sessao=FakeSessao({}), limite=0, delay=0, dry_run=True, prune=True
    )
    assert stats["orfas"] == 0
