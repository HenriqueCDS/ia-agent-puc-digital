"""Dependências injetáveis das rotas.

`get_assuntos_validos` bate no banco a cada chamada — sem cache de propósito:
a coleção tem hoje só 2 assuntos, a query é um DISTINCT indexado (~mesma
ordem de grandeza da busca por similaridade, que já é rápida), e cachear
abriria uma janela em que um assunto recém-apagado (`scripts/remove_ingested`)
continuaria "válido" para quem chama a API.
"""

import logging
from secrets import compare_digest

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app.api.errors import NaoAutenticado
from app.api.ratelimit import get_rate_limiter
from app.core.config import settings
from app.db.vector_store import get_vector_store, list_assuntos

logger = logging.getLogger(__name__)

# `auto_error=False`: sem isto o FastAPI devolve um 403 próprio, fora do
# envelope de erro de `errors.py`, e quem integra recebe dois formatos de erro
# diferentes da mesma API. Aqui a ausência do header vira `None` e nós é que
# decidimos o status (401) e o corpo.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_request_id(request: Request) -> str:
    """Lido do `request.state` que o middleware de `app/api/app.py` preenche."""
    return request.state.request_id


def get_assuntos_validos() -> list[str]:
    return list_assuntos(get_vector_store())


def get_consumidor(chave: str | None = Security(api_key_header)) -> str:
    """Identifica a integração pela `X-API-Key`, ou 401.

    Devolve o NOME do consumidor, não a chave: é ele que vai para a telemetria
    (`canal`) e para o rate limit. A chave nunca sai daqui — não deve acabar
    num log de acesso nem numa linha de telemetria.

    Fail-closed: com `API_AUTH_ENABLED=true` e nenhuma chave cadastrada, a
    rota responde 503 em vez de liberar o acesso. Um erro de configuração que
    abre o endpoint é pior que um que o derruba, porque não aparece em teste
    nenhum — só na fatura do Gemini.
    """
    if not settings.api_auth_enabled:
        return "anonimo"

    registradas = settings.api_keys_por_chave
    if not registradas:
        raise RuntimeError(
            "API_AUTH_ENABLED=true mas API_KEYS está vazia. Cadastre "
            "`nome:chave` no .env ou desligue a autenticação explicitamente."
        )

    if not chave:
        raise NaoAutenticado("Header X-API-Key ausente.")

    # Sem `dict.get`: comparação de string com saída antecipada vaza, pelo
    # tempo de resposta, quantos caracteres do prefixo estão certos. Percorre
    # todas as chaves sem interromper o laço.
    encontrado = None
    for registrada, nome in registradas.items():
        if compare_digest(chave, registrada):
            encontrado = nome

    if encontrado is None:
        logger.warning("chave de API inválida rejeitada")
        raise NaoAutenticado("Chave de API inválida.")

    return encontrado


def consumidor_com_rate_limit(consumidor: str = Depends(get_consumidor)) -> str:
    """`get_consumidor` + consumo de uma vaga na janela (ver `ratelimit.py`).

    Separado de `get_consumidor` porque nem toda rota autenticada deve gastar
    orçamento: `/assuntos` é um DISTINCT barato e a demo o chama a cada carga
    de página, enquanto `/ask` é o que custa dinheiro. Contar os dois no mesmo
    balde faria a página gastar a cota das perguntas.
    """
    get_rate_limiter().verificar(consumidor)
    return consumidor
