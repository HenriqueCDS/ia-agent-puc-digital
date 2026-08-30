"""Crawler da WEB_ALLOWLIST — sobe as páginas oficiais curadas para o pgvector.

Por que existe (KB-3, ver eval/analises/kb-3-melhorar-fallback-na-base.md): o
fallback ao vivo (`app/agent/web_fallback.py`) raspa o DuckDuckGo a CADA pergunta
que a base não cobre — ~15s, sujeito a rate limit. Este comando pré-indexa o
conteúdo das páginas da allowlist uma vez (e no re-crawl semanal), então a mesma
pergunta passa a ser respondida pelo RAG normal (~300ms, `grounded=True`,
cacheável). O fallback ao vivo continua no código, só como último recurso para
quando nem o conteúdo crawlado cobre.

    python -m scripts.crawl                     # todas as fontes com sitemap
    python -m scripts.crawl --host puc-campinas.edu.br
    python -m scripts.crawl --dry-run           # lista as URLs, não indexa
    python -m scripts.crawl --limite 40 --delay 1.5

CATEGORIA: cada página vira chunks com `source_type="web"` e `categoria="web"` na
metadata (é isso que separa conteúdo crawlado de PDF oficial). O `assunto`
continua sendo o da `FonteWeb` (`puc-digital`/`canvas`) e NÃO `"web"`, senão o
retrieval filtrado por assunto (`--assunto puc-digital`) não enxergaria a página.

ESCOPO: só entradas da `WEB_ALLOWLIST` cujo host responde a um sitemap. O Canvas
(`community.instructure.com`) fica de fora por padrão — os guias já estão
indexados como PDF e há ToS/robots a conferir; passe `--host` para forçar.
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.robotparser import RobotFileParser

import requests
import typer
from bs4 import BeautifulSoup
from langchain_core.documents import Document

from app.agent.web_fallback import fonte_permitida
from app.core.config import WEB_ALLOWLIST, FonteWeb
from app.ingestion.pipeline import ingest_documents

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="Indexa as páginas da WEB_ALLOWLIST no pgvector.")

_UA = "PUCDigitalAgentCrawler/1.0 (+https://www.puc-campinas.edu.br; suporte academico)"

# Caminhos de sitemap tentados na ordem. Yoast (o plugin de SEO do WordPress, que
# é o CMS do portal da PUC) publica em `/sitemap_index.xml`; o core do
# WordPress 5.5+ em `/wp-sitemap.xml`; `/sitemap.xml` cobre o resto.
_SITEMAPS = ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml")

# Página com menos texto que isto depois de tirar menu/rodapé é índice ou
# landing sem conteúdo — não vira chunk (mediria ruído de navegação).
_MIN_CHARS = 300

# Onde procurar o conteúdo principal, na ordem. `get_text` do `<body>` inteiro
# traria o menu lateral e o rodapé institucional em toda página.
_SELETORES_CONTEUDO = (
    "main",
    "article",
    ".entry-content",
    ".elementor-widget-container",
    "#content",
    ".site-content",
)

# Hosts crawlados quando nenhum `--host` é passado: os da PUC. O Canvas e o
# support.microsoft.com ficam de fora (ver docstring).
_HOSTS_PADRAO = tuple(
    f.host for f in WEB_ALLOWLIST if f.host.endswith("puc-campinas.edu.br")
)


def _sessao() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def _robots(sessao: requests.Session, host: str) -> RobotFileParser:
    """robots.txt do host. Ausente ou ilegível = tudo permitido (padrão da RFC)."""
    rp = RobotFileParser()
    try:
        resp = sessao.get(f"https://{host}/robots.txt", timeout=10)
        if resp.ok:
            rp.parse(resp.text.splitlines())
        else:
            rp.allow_all = True
    except requests.RequestException:
        rp.allow_all = True
    return rp


def _locs(xml_bytes: bytes) -> tuple[list[str], bool]:
    """Extrai os `<loc>` de um sitemap. Devolve (urls, é_índice_de_sitemaps)."""
    try:
        raiz = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [], False
    # O namespace do sitemap vem no tag ({http://.../sitemap/0.9}loc); casa por
    # sufixo para não depender da URL exata do schema.
    locs = [e.text.strip() for e in raiz.iter() if e.tag.endswith("}loc") and e.text]
    eh_indice = raiz.tag.endswith("}sitemapindex")
    return locs, eh_indice


def descobrir_urls(sessao: requests.Session, fonte: FonteWeb) -> list[str]:
    """URLs da `fonte` a partir do sitemap, já filtradas pelos `path_prefixes`.

    Só entra URL que a `fonte_permitida` (a MESMA revalidação da busca ao vivo)
    atribui a esta entrada da allowlist — sitemap não é confiado às cegas.
    """
    # Tenta o host nu e o `www.`: o portal da PUC serve o sitemap só em
    # `www.puc-campinas.edu.br` — o apex responde, mas com cert TLS inválido
    # (só cobre o `www`), o que derruba o `requests.get` com SSLError.
    hosts = [fonte.host]
    if not fonte.host.startswith("www."):
        hosts.append(f"www.{fonte.host}")
    a_visitar: list[str] = [f"https://{h}{c}" for h in hosts for c in _SITEMAPS]
    vistos_sitemap: set[str] = set()
    urls: set[str] = set()
    algum_respondeu = False

    while a_visitar:
        sm = a_visitar.pop(0)
        if sm in vistos_sitemap:
            continue
        vistos_sitemap.add(sm)
        try:
            resp = sessao.get(sm, timeout=15)
        except requests.RequestException as exc:
            logger.debug("sitemap %s falhou: %s", sm, exc)
            continue
        # Não filtra por content-type: Yoast serve `text/xml`, o core do WP
        # `application/xml`, e alguns proxies reescrevem para `text/plain`. O que
        # importa é o corpo ser XML de sitemap — `_locs` devolve [] se não for.
        if not resp.ok:
            continue

        locs, eh_indice = _locs(resp.content)
        if not locs:
            continue
        algum_respondeu = True
        if eh_indice:
            a_visitar.extend(locs)
            continue
        for u in locs:
            fp = fonte_permitida(u)
            if fp is fonte or (fp is not None and fp.host == fonte.host):
                urls.add(u.split("#")[0])

    if not algum_respondeu:
        typer.secho(
            f"  aviso: nenhum sitemap respondeu em {fonte.host} "
            f"(tentado: {', '.join(_SITEMAPS)}). Confira a URL do sitemap do site.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    return sorted(urls)


def _extrair(html: str) -> tuple[str, str]:
    """(título, texto principal) de uma página, sem menu/rodapé/scripts."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()

    titulo = soup.title.get_text(strip=True) if soup.title else ""

    principal = next(
        (soup.select_one(sel) for sel in _SELETORES_CONTEUDO if soup.select_one(sel)),
        soup.body or soup,
    )
    linhas = [ln.strip() for ln in principal.get_text("\n").splitlines() if ln.strip()]
    return titulo, "\n".join(linhas)


def _documento(url: str, titulo: str, texto: str, fonte: FonteWeb) -> Document:
    return Document(
        page_content=f"{titulo}\n\n{texto}".strip() if titulo else texto,
        metadata={
            # assunto = o da FonteWeb (para o filtro do retrieval); a marca de
            # "veio da web" é `source_type`/`categoria`, não o assunto.
            "assunto": fonte.assunto or "web",
            "categoria": "web",
            "source_type": "web",
            "source_uri": url,
            "source_path": url,   # estável entre execuções -> re-crawl substitui
            "source_name": url,   # RetrievedChunk.citation = a própria URL
            "titulo": titulo,
            "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def _crawl_fonte(
    fonte: FonteWeb,
    *,
    sessao: requests.Session,
    limite: int,
    delay: float,
    dry_run: bool,
) -> dict:
    rp = _robots(sessao, fonte.host)
    urls = descobrir_urls(sessao, fonte)
    if limite:
        urls = urls[:limite]

    typer.secho(
        f"\n{fonte.host}  ({', '.join(fonte.path_prefixes)})  ->  {len(urls)} URL(s)",
        fg=typer.colors.CYAN,
        bold=True,
    )
    if dry_run:
        for u in urls:
            typer.echo(f"  {u}")
        return {"urls": len(urls), "paginas": 0, "chunks": 0, "duplicados": 0, "pulados": 0}

    stats = {"urls": len(urls), "paginas": 0, "chunks": 0, "duplicados": 0, "pulados": 0}
    for u in urls:
        if not rp.can_fetch(_UA, u):
            logger.info("robots.txt bloqueia %s", u)
            stats["pulados"] += 1
            continue
        try:
            resp = sessao.get(u, timeout=15)
        except requests.RequestException as exc:
            logger.warning("falhou %s: %s", u, exc)
            stats["pulados"] += 1
            continue
        if not resp.ok or "html" not in resp.headers.get("content-type", ""):
            stats["pulados"] += 1
            continue

        titulo, texto = _extrair(resp.text)
        if len(texto) < _MIN_CHARS:
            logger.info("pouco conteúdo (%d chars), pulado: %s", len(texto), u)
            stats["pulados"] += 1
            time.sleep(delay)
            continue

        novos, dup = ingest_documents([_documento(u, titulo, texto, fonte)], u)
        stats["paginas"] += 1
        stats["chunks"] += novos
        stats["duplicados"] += dup
        logger.info("%s -> %d chunk(s)%s", u, novos, f", {dup} dup" if dup else "")
        time.sleep(delay)

    return stats


@app.command()
def main(
    host: list[str] = typer.Option(
        None, "--host", "-h", help="Host(s) da allowlist a crawlar. Padrão: os da PUC."
    ),
    limite: int = typer.Option(0, help="Teto de páginas por fonte (0 = sem teto)."),
    delay: float = typer.Option(1.0, help="Pausa entre requisições, em segundos (educação com o servidor)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Só lista as URLs que entrariam."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log página a página."),
) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")

    alvos = set(host) if host else set(_HOSTS_PADRAO)
    fontes = [f for f in WEB_ALLOWLIST if f.host in alvos]
    if not fontes:
        disponiveis = ", ".join(f.host for f in WEB_ALLOWLIST)
        typer.secho(f"Nenhuma FonteWeb com host em {sorted(alvos)}. Disponíveis: {disponiveis}",
                    fg=typer.colors.RED)
        raise typer.Exit(code=1)

    sessao = _sessao()
    total = {"urls": 0, "paginas": 0, "chunks": 0, "duplicados": 0, "pulados": 0}
    for fonte in fontes:
        s = _crawl_fonte(fonte, sessao=sessao, limite=limite, delay=delay, dry_run=dry_run)
        for k in total:
            total[k] += s[k]

    if dry_run:
        typer.secho(f"\n{total['urls']} URL(s) no total (dry-run, nada indexado).", bold=True)
        return

    typer.secho(
        f"\n{total['paginas']} página(s) indexada(s), {total['chunks']} chunk(s), "
        f"{total['duplicados']} duplicado(s), {total['pulados']} pulada(s).",
        fg=typer.colors.GREEN,
        bold=True,
    )


if __name__ == "__main__":
    app()
