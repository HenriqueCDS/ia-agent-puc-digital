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
    python -m scripts.crawl --prune             # re-crawl + remove do índice a página que saiu do sitemap (KB-5)

O job semanal (`.github/workflows/recrawl.yml`) roda `--prune`. O prune só age
numa fonte cujo sitemap respondeu por completo, e pára se fosse apagar mais da
metade das páginas indexadas dela (--prune-force ignora esse teto).

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
from app.db.vector_store import delete_by_source, get_vector_store, list_web_sources
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

# Prune (KB-5): se apagaria MAIS que esta fração das páginas já indexadas da
# fonte, ele pára e exige --prune-force. Um sitemap que responde mas com outro
# esquema de URL (site migrado, domínio novo) transforma todas as páginas
# antigas em "órfãs" de uma vez — quase sempre é redirecionamento, não remoção.
# Melhor um re-crawl que não limpa do que um que zera o índice.
_PRUNE_FRACAO_MAX = 0.5

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


def _habilitar_trust_store_do_so() -> None:
    """Faz o `requests` confiar nas CAs do SO (Windows/macOS).

    Redes com proxy TLS corporativo apresentam um certificado assinado por uma
    CA que só está no trust store do sistema, não no bundle do `certifi` que o
    `requests` usa por padrão — o resultado é `CERTIFICATE_VERIFY_FAILED` e
    nenhum sitemap "responde". `truststore` redireciona a verificação para o SO.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        logger.debug("truststore ausente; usando o bundle do certifi")


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


def descobrir_urls(sessao: requests.Session, fonte: FonteWeb) -> tuple[list[str], bool]:
    """URLs da `fonte` a partir do sitemap, já filtradas pelos `path_prefixes`.

    Devolve `(urls, confiavel)`. `confiavel` é `True` só quando algum sitemap
    respondeu E nenhum sub-sitemap descoberto por um índice falhou ao ser
    baixado/parseado — é a pré-condição do prune (`_podar_orfas`): sem essa
    garantia, uma página que só está fora da lista por causa de um sub-sitemap
    que deu 500 seria apagada do índice por engano (KB-5).

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
    # Os 6 candidatos iniciais são CHUTES — a maioria dá 404 e isso é esperado,
    # não falha. Só conta como falha um sub-sitemap que veio de um índice e não
    # respondeu: aí faltam URLs de verdade e o prune não pode rodar.
    candidatos_iniciais = set(a_visitar)
    vistos_sitemap: set[str] = set()
    urls: set[str] = set()
    algum_respondeu = False
    subsitemap_falhou = False

    while a_visitar:
        sm = a_visitar.pop(0)
        if sm in vistos_sitemap:
            continue
        vistos_sitemap.add(sm)
        de_indice = sm not in candidatos_iniciais
        try:
            resp = sessao.get(sm, timeout=15)
        except requests.RequestException as exc:
            logger.debug("sitemap %s falhou: %s", sm, exc)
            subsitemap_falhou |= de_indice
            continue
        # Não filtra por content-type: Yoast serve `text/xml`, o core do WP
        # `application/xml`, e alguns proxies reescrevem para `text/plain`. O que
        # importa é o corpo ser XML de sitemap — `_locs` devolve [] se não for.
        if not resp.ok:
            subsitemap_falhou |= de_indice
            continue

        locs, eh_indice = _locs(resp.content)
        if not locs:
            subsitemap_falhou |= de_indice
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
    return sorted(urls), (algum_respondeu and not subsitemap_falhou)


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


def _podar_orfas(
    fonte: FonteWeb, vivas: set[str], *, dry_run: bool, forcar: bool
) -> dict:
    """Remove do índice as páginas desta fonte que sumiram do sitemap (KB-5).

    `vivas` = as URLs que `descobrir_urls` acabou de devolver (sitemap ∩
    `path_prefixes`, já revalidadas por `fonte_permitida`). Qualquer
    `source_path` com `source_type='web'` que pertença a esta fonte e não esteja
    em `vivas` é órfão: a página foi despublicada, mudou de slug ou saiu do ar,
    e sem esta poda seus chunks ficam no índice para sempre — respondendo
    `grounded=true` com citação que dá 404.

    `delete_by_source` é seguro para isto: escopo por `source_path` exato, e só
    é chamado para URL que ESTA fonte reconhece como sua. Quem garante que
    `vivas` está completa é o `confiavel` de `descobrir_urls` (checado por quem
    chama) + o teto de `_PRUNE_FRACAO_MAX`.
    """
    store = get_vector_store()
    indexadas = [
        (sp, n)
        for sp, n in list_web_sources(store)
        if (fp := fonte_permitida(sp)) is not None and fp.host == fonte.host
    ]
    orfas = [(sp, n) for sp, n in indexadas if sp not in vivas]
    if not orfas:
        return {"orfas": 0, "chunks_podados": 0}

    fracao = len(orfas) / len(indexadas)
    if fracao > _PRUNE_FRACAO_MAX and not forcar:
        typer.secho(
            f"  prune ABORTADO em {fonte.host}: {len(orfas)}/{len(indexadas)} páginas "
            f"({fracao:.0%}) fora do sitemap — parece migração de URL, não remoção. "
            "Rode --dry-run para conferir e --prune-force para confirmar.",
            fg=typer.colors.RED,
            err=True,
        )
        return {"orfas": len(orfas), "chunks_podados": 0}

    for sp, n in orfas:
        marca = "[dry-run] " if dry_run else ""
        typer.secho(f"  {marca}órfã ({n} chunk(s)): {sp}", fg=typer.colors.YELLOW)
    if dry_run:
        return {"orfas": len(orfas), "chunks_podados": 0}

    removidos = sum(delete_by_source(store, sp) for sp, _ in orfas)
    return {"orfas": len(orfas), "chunks_podados": removidos}


def _crawl_fonte(
    fonte: FonteWeb,
    *,
    sessao: requests.Session,
    limite: int,
    delay: float,
    dry_run: bool,
    prune: bool = False,
    prune_force: bool = False,
) -> dict:
    rp = _robots(sessao, fonte.host)
    urls_todas, sitemap_confiavel = descobrir_urls(sessao, fonte)
    urls = urls_todas[:limite] if limite else urls_todas

    de_quantas = "" if not limite else f" (de {len(urls_todas)} no sitemap)"
    typer.secho(
        f"\n{fonte.host}  ({', '.join(fonte.path_prefixes)})  ->  {len(urls)} URL(s){de_quantas}",
        fg=typer.colors.CYAN,
        bold=True,
    )

    stats = {
        "urls": len(urls), "paginas": 0, "chunks": 0, "duplicados": 0,
        "pulados": 0, "orfas": 0, "chunks_podados": 0,
    }

    if dry_run:
        for u in urls:
            typer.echo(f"  {u}")
    else:
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

    if prune:
        if not sitemap_confiavel:
            typer.secho(
                f"  prune PULADO em {fonte.host}: nem todos os sitemaps responderam — "
                "apagar agora removeria página que só está fora da lista por falha de rede.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        elif not urls_todas:
            typer.secho(
                f"  prune PULADO em {fonte.host}: sitemap sem nenhuma URL nos path_prefixes.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        else:
            # Compara sempre contra o sitemap INTEIRO (`urls_todas`), nunca contra
            # o recorte de `--limite` — senão a 41ª página viraria "órfã".
            poda = _podar_orfas(
                fonte, set(urls_todas), dry_run=dry_run, forcar=prune_force
            )
            stats["orfas"] = poda["orfas"]
            stats["chunks_podados"] = poda["chunks_podados"]

    return stats


@app.command()
def main(
    host: list[str] = typer.Option(
        None, "--host", "-h", help="Host(s) da allowlist a crawlar. Padrão: os da PUC."
    ),
    limite: int = typer.Option(0, help="Teto de páginas por fonte (0 = sem teto)."),
    delay: float = typer.Option(1.0, help="Pausa entre requisições, em segundos (educação com o servidor)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Só lista as URLs (e as órfãs, com --prune). Não indexa nem apaga."),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Depois de indexar, remove do índice as páginas que sumiram do sitemap (KB-5). "
        "É o que o job semanal usa. Não roda numa fonte cujo sitemap não respondeu por completo.",
    ),
    prune_force: bool = typer.Option(
        False,
        "--prune-force",
        help="Confirma o prune mesmo quando mais da metade das páginas indexadas sumiu do "
        "sitemap (em geral sinal de migração de URL, não remoção).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log página a página."),
) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")
    _habilitar_trust_store_do_so()

    alvos = set(host) if host else set(_HOSTS_PADRAO)
    fontes = [f for f in WEB_ALLOWLIST if f.host in alvos]
    if not fontes:
        disponiveis = ", ".join(f.host for f in WEB_ALLOWLIST)
        typer.secho(f"Nenhuma FonteWeb com host em {sorted(alvos)}. Disponíveis: {disponiveis}",
                    fg=typer.colors.RED)
        raise typer.Exit(code=1)

    sessao = _sessao()
    total = {
        "urls": 0, "paginas": 0, "chunks": 0, "duplicados": 0,
        "pulados": 0, "orfas": 0, "chunks_podados": 0,
    }
    for fonte in fontes:
        s = _crawl_fonte(
            fonte, sessao=sessao, limite=limite, delay=delay,
            dry_run=dry_run, prune=prune, prune_force=prune_force,
        )
        for k in total:
            total[k] += s[k]

    if dry_run:
        extra = f" — {total['orfas']} órfã(s) seriam removidas" if prune else ""
        typer.secho(
            f"\n{total['urls']} URL(s) no total (dry-run, nada indexado){extra}.", bold=True
        )
        return

    poda = (
        f" {total['orfas']} órfã(s) removida(s) ({total['chunks_podados']} chunk(s))."
        if prune else ""
    )
    typer.secho(
        f"\n{total['paginas']} página(s) indexada(s), {total['chunks']} chunk(s), "
        f"{total['duplicados']} duplicado(s), {total['pulados']} pulada(s).{poda}",
        fg=typer.colors.GREEN,
        bold=True,
    )


if __name__ == "__main__":
    app()
