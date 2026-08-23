"""Frontend de demonstração (T3.1): um HTML estático servido pela própria API.

Mesma origem que a v1 de propósito — a demo não precisa de CORS (T2.2 continua
existindo para o AVA de verdade, que é cross-origin) e não há um segundo
servidor para subir junto.

SOBRE A CHAVE (decisão 3 do BACKLOG, opção (a)): com T2.1 no lugar, um HTML que
chama `/v1/ask` via `fetch` precisa de uma `X-API-Key`, e qualquer chave que o
JS use é pública para quem abrir o DevTools. Isso não tem solução dentro de um
arquivo estático — o que dá para controlar é o ESTRAGO:

- a chave é de uma integração própria (`DEMO_CONSUMIDOR`, default "demo"), então
  revogá-la não derruba o AVA;
- ela tem teto diário próprio (`RATE_LIMIT_DIARIO_POR_CONSUMIDOR`), então quem
  abusar dela gasta o orçamento da demo, não o do dia inteiro;
- ela é injetada aqui, no servidor, e não commitada no HTML — o arquivo em
  `app/static/` tem só o placeholder.

A alternativa (b), um cookie de sessão emitido por `/demo`, foi descartada: cria
um segundo mecanismo de autenticação para manter e testar, e resolve um
problema (chave visível) que os tetos acima já contêm.
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_INDEX = Path(__file__).resolve().parents[2] / "static" / "index.html"

# Servido em `/static` (ver `app/api/app.py`). Subpasta própria, e não
# `app/static/` inteira, para que o `index.html` não fique acessível por fora do
# `/demo` — servido direto ele viria com o placeholder da chave no lugar dela, e
# a página carregaria quebrada sem dizer por quê.
IMAGENS = _INDEX.parent / "img"

# O placeholder inclui as aspas: a substituição é por `json.dumps`, que devolve
# um literal JS já escapado. Substituir só o miolo deixaria a porta aberta para
# uma chave com aspas quebrar (ou escapar de) o script.
_PLACEHOLDER = '"__API_KEY__"'


def _html_da_demo() -> str:
    """Lê o HTML e injeta a chave. Sem cache de propósito.

    O arquivo é lido a cada carga de página: são ~30 KB de disco (page cache do
    SO, na prática) contra o custo de reiniciar o servidor para ver uma
    mudança de CSS. A demo não é o caminho quente — `/v1/ask` é.
    """
    html = _INDEX.read_text(encoding="utf-8")

    chave = ""
    if settings.api_auth_enabled:
        chave = settings.chave_do_consumidor(settings.demo_consumidor) or ""

    return html.replace(_PLACEHOLDER, json.dumps(chave))


@router.get("/demo", response_class=HTMLResponse, include_in_schema=False)
def demo() -> HTMLResponse:
    # `no-store`: a resposta tem uma chave de API dentro. Um proxy intermediário
    # ou o cache do navegador guardando isso é como a chave sobrevive a uma
    # rotação — e reaparece depois de a antiga ter sido revogada.
    return HTMLResponse(_html_da_demo(), headers={"Cache-Control": "no-store"})


@router.get("/", include_in_schema=False)
def raiz() -> RedirectResponse:
    """Atalho: quem abre a raiz do serviço quer ver a demo, não um 404.

    307 e não 301: um permanente fica gravado no navegador e continuaria
    redirecionando depois de a demo ser desligada em produção.
    """
    return RedirectResponse("/demo", status_code=307)
