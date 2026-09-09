"""Testes de `app/db/perguntas_store.py` — sem Postgres.

O que se trava aqui é o que não depende de uma conexão: os helpers puros
(normalização, validação, montagem de params) e as INVARIANTES entre o Python e
o SQL — o domínio de `origem_esperada` tem que casar `models.Origem`, e o hash
tem que ser o MESMO de `telemetry`, senão a junção com a telemetria quebra em
silêncio.
"""

import re

import pytest

from app.core import telemetry
from app.core.models import Origem
from app.db import perguntas_store as ps


def test_dominio_de_origem_casa_models_origem():
    """`ORIGENS_VALIDAS` é reescrito como literal SQL num CHECK — não pode
    divergir de `models.Origem` (o contrato que o agente de fato devolve)."""
    assert set(ps.ORIGENS_VALIDAS) == set(Origem.__args__)


def test_hash_e_o_mesmo_da_telemetria():
    """A junção pergunta × telemetria é por `pergunta_hash`. Se este módulo
    calcular o hash diferente de `telemetry.hash_pergunta`, a revisão individual
    para de casar as duas pontas e ninguém vê erro."""
    params = ps._params({"pergunta": "  Como Envio Uma Atividade? ", "origem_esperada": "base"})
    assert params["pergunta_hash"] == telemetry.hash_pergunta("Como Envio Uma Atividade?")


def test_ddl_declara_a_chave_natural_grupo_hash():
    """A idempotência do seed depende do índice único `(grupo, pergunta_hash)`.
    Uma refatoração que o tire (ou troque para só o hash) reintroduz o bug de
    a mesma pergunta em dois grupos colidir."""
    ddl = " ".join(str(i) for i in ps._INDICES)
    assert re.search(r"UNIQUE INDEX.*\(grupo, pergunta_hash\)", ddl)


def test_normalizar_tambem_ok_deduplica_e_ordena():
    assert ps._normalizar_tambem_ok(["web", "nenhuma", "web"]) == ["nenhuma", "web"]
    assert ps._normalizar_tambem_ok(None) == []
    assert ps._normalizar_tambem_ok([" base "]) == ["base"]


def test_normalizar_tambem_ok_rejeita_valor_fora_do_dominio():
    with pytest.raises(ValueError, match="origem_tambem_ok inválida"):
        ps._normalizar_tambem_ok(["base", "inventada"])


def test_params_valida_pergunta_e_origem():
    with pytest.raises(ValueError, match="vazia"):
        ps._params({"pergunta": "   ", "origem_esperada": "base"})
    with pytest.raises(ValueError, match="origem_esperada inválida"):
        ps._params({"pergunta": "ok?", "origem_esperada": "talvez"})


def test_params_normaliza_campos_opcionais():
    p = ps._params(
        {"pergunta": "P?", "origem_esperada": "web", "grupo": " teste2 ",
         "assunto": "", "criterio": "", "origem_tambem_ok": ["nenhuma"]}
    )
    assert p["grupo"] == "teste2"
    assert p["assunto"] is None and p["criterio"] is None
    assert p["origem_tambem_ok"] == ["nenhuma"]


def test_como_item_devolve_o_formato_que_o_eval_run_consome():
    item = ps.PerguntaExemplo(
        id=1, grupo="teste", pergunta="P?", pergunta_hash="abc", assunto=None,
        origem_esperada="base", origem_tambem_ok=[], criterio=None, ativo=True,
    ).como_item()
    # `origem_tambem_ok` vazio vira None — é como o JSONC representava "não tem",
    # e `eval_run._linha` grava esse campo no resultado.
    assert item == {
        "grupo": "teste", "pergunta": "P?", "assunto": None,
        "origem_esperada": "base", "origem_tambem_ok": None, "criterio": None,
    }


def test_como_item_grupo_vazio_vira_none():
    item = ps.PerguntaExemplo(
        id=1, grupo="", pergunta="P?", pergunta_hash="abc", assunto=None,
        origem_esperada="base", origem_tambem_ok=["web"], criterio="x", ativo=True,
    ).como_item()
    assert item["grupo"] is None
    assert item["origem_tambem_ok"] == ["web"]


def test_resumo_upsert_total():
    assert ps.ResumoUpsert(inseridos=2, atualizados=1, inalterados=47).total == 50


def test_atualizar_ignora_campos_nao_editaveis(monkeypatch):
    """`pergunta_hash` não pode vir de fora — é derivado da pergunta. Um PATCH
    que tente setá-lo direto é silenciosamente ignorado."""
    capturado = {}

    def fake_obter(id_, store=None):
        return ps.PerguntaExemplo(
            id=id_, grupo="t", pergunta="P?", pergunta_hash="orig", assunto=None,
            origem_esperada="base", origem_tambem_ok=[], criterio=None, ativo=True,
        )

    monkeypatch.setattr(ps, "obter", fake_obter)
    monkeypatch.setattr(ps, "_ensure_table", lambda store=None: None)
    monkeypatch.setattr(ps, "get_vector_store", lambda: (_ for _ in ()).throw(AssertionError("tocou no banco")))

    # Só campos proibidos → nada a fazer, devolve o atual sem abrir sessão.
    resultado = ps.atualizar(1, {"pergunta_hash": "forjado", "id": 9})
    assert resultado.pergunta_hash == "orig"
