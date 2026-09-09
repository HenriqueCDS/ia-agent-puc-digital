"""CRUD do dataset de avaliação — tabela `exemplo_perguntas`.

A leitura (`GET`) serve a tela `/revisao` e o `scripts.eval_run`; vale para
qualquer integração autenticada. A escrita (`POST`/`PATCH`/`DELETE`) muda o
dataset que decide se uma próxima rodada passa ou falha, então fica atrás de
`consumidor_de_escrita_de_avaliacao` — só o consumidor nomeado em
`PERGUNTAS_CONSUMIDOR_ESCRITA`, nunca a chave da demo (que é pública no HTML).

DELETE é lógico (`ativo=false`): ver a docstring de `perguntas_store.desativar`
— apagar a linha de verdade deixaria órfãos os registros de telemetria de
rodadas antigas que referenciam o `pergunta_hash` dela.

A rota inteira sai do ar com `PERGUNTAS_CRUD_ENABLED=false`.
"""

import logging

from fastapi import APIRouter, Depends

from app.api.deps import consumidor_de_escrita_de_avaliacao, get_consumidor
from app.api.errors import RecursoNaoEncontrado
from app.api.schemas import (
    PerguntaCreate,
    PerguntaOut,
    PerguntasListOut,
    PerguntaUpdate,
    pergunta_out_de,
)
from app.db import perguntas_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/perguntas", tags=["avaliacao"])


@router.get("", response_model=PerguntasListOut)
def listar(
    grupo: str | None = None,
    incluir_inativas: bool = False,
    consumidor: str = Depends(get_consumidor),
) -> PerguntasListOut:
    perguntas = perguntas_store.listar(grupo=grupo, apenas_ativas=not incluir_inativas)
    return PerguntasListOut(
        perguntas=[pergunta_out_de(p) for p in perguntas], total=len(perguntas)
    )


@router.get("/{id_}", response_model=PerguntaOut)
def obter(id_: int, consumidor: str = Depends(get_consumidor)) -> PerguntaOut:
    p = perguntas_store.obter(id_)
    if p is None:
        raise RecursoNaoEncontrado(f"pergunta {id_} não existe")
    return pergunta_out_de(p)


@router.post("", response_model=PerguntaOut, status_code=201)
def criar(
    corpo: PerguntaCreate,
    consumidor: str = Depends(consumidor_de_escrita_de_avaliacao),
) -> PerguntaOut:
    # `criar` reaproveita o UPSERT do seed: se a chave natural
    # `(grupo, pergunta_hash)` já existir (mesmo texto, mesmo grupo, talvez
    # desativada), o efeito é reativar/atualizar em vez de um 409 que forçaria
    # quem integra a descobrir o `id` primeiro. Ver a docstring de
    # `perguntas_store.criar`.
    p = perguntas_store.criar(corpo.model_dump())
    logger.info("consumidor %r criou/atualizou pergunta %d (%s)", consumidor, p.id, p.grupo)
    return pergunta_out_de(p)


@router.patch("/{id_}", response_model=PerguntaOut)
def atualizar(
    id_: int,
    corpo: PerguntaUpdate,
    consumidor: str = Depends(consumidor_de_escrita_de_avaliacao),
) -> PerguntaOut:
    # `exclude_unset`: um PATCH que manda só `{"origem_esperada": "nenhuma"}`
    # não pode zerar `criterio` só porque o campo tem default `None` no schema.
    campos = corpo.model_dump(exclude_unset=True)
    p = perguntas_store.atualizar(id_, campos)
    if p is None:
        raise RecursoNaoEncontrado(f"pergunta {id_} não existe")
    logger.info("consumidor %r editou pergunta %d: %s", consumidor, id_, sorted(campos))
    return pergunta_out_de(p)


@router.delete("/{id_}", status_code=204)
def desativar(
    id_: int,
    consumidor: str = Depends(consumidor_de_escrita_de_avaliacao),
) -> None:
    if perguntas_store.obter(id_) is None:
        raise RecursoNaoEncontrado(f"pergunta {id_} não existe")
    # Idempotente: desativar de novo devolve 204 igual. `desativar` só mexe se
    # ainda estiver ativa.
    perguntas_store.desativar(id_)
    logger.info("consumidor %r desativou pergunta %d", consumidor, id_)
