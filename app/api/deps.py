"""Dependências injetáveis das rotas.

`get_assuntos_validos` bate no banco a cada chamada — sem cache de propósito:
a coleção tem hoje só 2 assuntos, a query é um DISTINCT indexado (~mesma
ordem de grandeza da busca por similaridade, que já é rápida), e cachear
abriria uma janela em que um assunto recém-apagado (`scripts/remove_ingested`)
continuaria "válido" para quem chama a API.
"""

from fastapi import Request

from app.db.vector_store import get_vector_store, list_assuntos


def get_request_id(request: Request) -> str:
    """Lido do `request.state` que o middleware de `app/api/app.py` preenche."""
    return request.state.request_id


def get_assuntos_validos() -> list[str]:
    return list_assuntos(get_vector_store())
