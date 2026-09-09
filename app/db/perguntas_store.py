"""Dataset de avaliação na mesma base Postgres da ingestão (tabela `exemplo_perguntas`).

Antes o dataset era só `eval/perguntas/perguntas.jsonc`, lido a cada rodada de
`scripts.eval_run`. O arquivo continua versionado como SEMENTE (é o que entra
num checkout novo, e o histórico dele é revisável em diff); a fonte VIVA passa
a ser esta tabela — é o que permite corrigir uma expectativa pela tela de
revisão e pela API sem reescrever um arquivo curado, cheio de comentários.

Tabela própria e isolada aqui, mesmo motivo de `response_cache.py` e
`telemetry_store.py`: se o acesso a ela mudar, só este módulo é afetado. SQL
cru, sem ORM e sem Alembic — o projeto inteiro cria tabela com
`CREATE TABLE IF NOT EXISTS` e migra com `ADD COLUMN IF NOT EXISTS`.

`origem_tambem_ok` é TEXT[] e não JSONB (a escolha oposta à da `telemetria`).
JSONB se paga quando a FORMA do dado ainda pode mudar — é o caso do registro de
telemetria, que ganha campo a cada feature. Aqui a forma é fechada: uma lista
homogênea de rótulos de `models.Origem`, quatro valores possíveis. Array dá
`@>` / `= ANY` direto e um CHECK de domínio de graça; JSONB daria flexibilidade
que ninguém vai usar em troca de cast em toda consulta.

CHAVE NATURAL — `(grupo, pergunta_hash)`, e não `pergunta_hash` sozinho. O
dataset REPETE perguntas de propósito entre grupos ("Quais dicas a instituição
dá para organizar os estudos orientados?" está em `teste` e em `teste2`, a
segunda com `criterio` de regressão). Chavear só pelo hash faria o seed da
segunda sobrescrever a primeira, e o resumo mentiria sobre o total.

`pergunta_hash` sai de `telemetry.hash_pergunta` — a MESMA função da
telemetria. É deliberado: a `telemetria` nunca guarda o texto da pergunta (ver
o cabeçalho de app/core/telemetry.py), então este hash é a única ponte entre "o
que o agente respondeu" e "que pergunta era". Trocar a função aqui sem trocar lá
quebra a revisão individual em silêncio — `test_perguntas_store` trava isso.
"""

import logging

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.core import telemetry
from app.db.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# Espelha `models.Origem`. Repetido aqui como literal SQL porque um CHECK do
# Postgres não importa Python — e é por isso que o teste trava a igualdade.
ORIGENS_VALIDAS = ("base", "web", "encaminhado", "nenhuma")

_LISTA_SQL = ", ".join(f"'{o}'" for o in ORIGENS_VALIDAS)

_CRIAR = text(
    f"""
    CREATE TABLE IF NOT EXISTS exemplo_perguntas (
        id BIGSERIAL PRIMARY KEY,
        grupo TEXT NOT NULL DEFAULT '',
        pergunta TEXT NOT NULL,
        pergunta_hash TEXT NOT NULL,
        assunto TEXT,
        origem_esperada TEXT NOT NULL,
        origem_tambem_ok TEXT[] NOT NULL DEFAULT '{{}}',
        criterio TEXT,
        ativo BOOLEAN NOT NULL DEFAULT TRUE,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT exemplo_perguntas_origem_valida
            CHECK (origem_esperada IN ({_LISTA_SQL})),
        CONSTRAINT exemplo_perguntas_tambem_ok_valida
            CHECK (origem_tambem_ok <@ ARRAY[{_LISTA_SQL}]::TEXT[])
    )
    """
)

# UNIQUE como índice separado (e não constraint inline) por causa do
# `IF NOT EXISTS`: `ALTER TABLE ... ADD CONSTRAINT` não é idempotente no
# Postgres, e o CREATE TABLE acima é no-op numa base que já existe.
_INDICES = (
    text(
        "CREATE UNIQUE INDEX IF NOT EXISTS exemplo_perguntas_chave_idx "
        "ON exemplo_perguntas (grupo, pergunta_hash)"
    ),
    text(
        "CREATE INDEX IF NOT EXISTS exemplo_perguntas_hash_idx "
        "ON exemplo_perguntas (pergunta_hash)"
    ),
    text("CREATE INDEX IF NOT EXISTS exemplo_perguntas_grupo_idx ON exemplo_perguntas (grupo)"),
)

# Checa a tabela E o índice único: numa base criada por uma versão anterior a
# este módulo o CREATE TABLE seria no-op e o índice nunca apareceria. Mesmo
# raciocínio de `response_cache._TABELA_PRONTA`, que checa a coluna `modelo`.
_TABELA_PRONTA = text(
    """
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'exemplo_perguntas'
      AND indexname = 'exemplo_perguntas_chave_idx'
    """
)

_COLUNAS = (
    "id, grupo, pergunta, pergunta_hash, assunto, origem_esperada, "
    "origem_tambem_ok, criterio, ativo, criado_em, atualizado_em"
)


@dataclass(frozen=True)
class PerguntaExemplo:
    """Um item do dataset de avaliação.

    Espelha o que `eval_run._carregar_dataset` devolvia por item, mais a
    identidade (`id`, `pergunta_hash`) e o ciclo de vida (`ativo`, timestamps)
    que só existem porque agora o dataset é editável fora do arquivo.
    """

    id: int
    grupo: str
    pergunta: str
    pergunta_hash: str
    assunto: str | None
    origem_esperada: str
    origem_tambem_ok: list[str]
    criterio: str | None
    ativo: bool
    criado_em: datetime | None = None
    atualizado_em: datetime | None = None

    def como_item(self) -> dict:
        """No formato que `scripts.eval_run` já consome (`_linha`, `_origens_aceitas`).

        Existe para que ligar o banco NÃO exija reescrever o harness: o resto do
        eval_run continua recebendo o mesmo dicionário que lia do JSONC.
        `origem_tambem_ok` vazio vira `None` porque é assim que o arquivo
        representa "não tem" — e `_linha` grava esse campo no resultado.
        """
        return {
            "grupo": self.grupo or None,
            "pergunta": self.pergunta,
            "assunto": self.assunto,
            "origem_esperada": self.origem_esperada,
            "origem_tambem_ok": list(self.origem_tambem_ok) or None,
            "criterio": self.criterio,
        }


def _de_linha(linha) -> PerguntaExemplo:
    return PerguntaExemplo(
        id=linha.id,
        grupo=linha.grupo or "",
        pergunta=linha.pergunta,
        pergunta_hash=linha.pergunta_hash,
        assunto=linha.assunto,
        origem_esperada=linha.origem_esperada,
        origem_tambem_ok=list(linha.origem_tambem_ok or []),
        criterio=linha.criterio,
        ativo=linha.ativo,
        criado_em=getattr(linha, "criado_em", None),
        atualizado_em=getattr(linha, "atualizado_em", None),
    )


def _ensure_table(store=None) -> None:
    """Cria tabela e índices se faltarem. Sem cache de processo (INF-11).

    Uma consulta ao catálogo por acesso custa ~µs e some com a classe de bug em
    que uma tabela dropada em runtime (teste que zera a base, manutenção) quebra
    todo acesso seguinte até o restart do processo.
    """
    store = store or get_vector_store()
    with store.session_maker() as sessao:
        if sessao.execute(_TABELA_PRONTA).first():
            return
        sessao.execute(_CRIAR)
        for indice in _INDICES:
            sessao.execute(indice)
        sessao.commit()


def _normalizar_tambem_ok(valores) -> list[str]:
    """Deduplica, ordena e valida contra `ORIGENS_VALIDAS`.

    Ordenar importa para o seed ser idempotente: `["web","nenhuma"]` e
    `["nenhuma","web"]` são a mesma expectativa, e sem a normalização o UPDATE
    acharia diferença a cada execução e mexeria em `atualizado_em` — que é o
    sinal que alguém usa para saber o que mudou de verdade.
    """
    limpos = sorted({str(v).strip() for v in (valores or []) if str(v).strip()})
    invalidos = [v for v in limpos if v not in ORIGENS_VALIDAS]
    if invalidos:
        raise ValueError(
            f"origem_tambem_ok inválida: {invalidos}. Válidas: {list(ORIGENS_VALIDAS)}"
        )
    return limpos


def _validar(pergunta: str, origem_esperada: str) -> None:
    if not (pergunta or "").strip():
        raise ValueError("pergunta não pode ser vazia")
    if origem_esperada not in ORIGENS_VALIDAS:
        raise ValueError(
            f"origem_esperada inválida: {origem_esperada!r}. Válidas: {list(ORIGENS_VALIDAS)}"
        )


# --- leitura ----------------------------------------------------------------


def listar(
    grupo: str | None = None,
    apenas_ativas: bool = True,
    store=None,
) -> list[PerguntaExemplo]:
    """O dataset, na ordem de `id` — que é a ordem em que o seed inseriu.

    Ordem estável e não `ORDER BY pergunta`: é o que faz `--intervalo 27-50` do
    eval_run continuar significando o mesmo trecho entre duas rodadas, que é a
    única razão de aquela flag existir.
    """
    _ensure_table(store)
    condicoes, params = [], {}
    if grupo is not None:
        condicoes.append("grupo = :grupo")
        params["grupo"] = grupo
    if apenas_ativas:
        condicoes.append("ativo")
    onde = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""

    with (store or get_vector_store()).session_maker() as sessao:
        linhas = sessao.execute(
            text(f"SELECT {_COLUNAS} FROM exemplo_perguntas {onde} ORDER BY id"), params
        ).all()
    return [_de_linha(linha) for linha in linhas]


def obter(id_: int, store=None) -> PerguntaExemplo | None:
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        linha = sessao.execute(
            text(f"SELECT {_COLUNAS} FROM exemplo_perguntas WHERE id = :id"), {"id": id_}
        ).first()
    return _de_linha(linha) if linha else None


def por_hash(hashes: list[str], store=None) -> dict[str, list[PerguntaExemplo]]:
    """`{pergunta_hash: [itens]}` — a ponte com a `telemetria`.

    O valor é uma LISTA, não um item: o mesmo texto pode estar em mais de um
    grupo (ver a nota de chave natural no topo). Quem consome decide o que fazer
    com o empate; devolver um item só faria a tela mostrar a expectativa de um
    grupo enquanto o contador diz outro.
    """
    if not hashes:
        return {}
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        linhas = sessao.execute(
            text(
                f"SELECT {_COLUNAS} FROM exemplo_perguntas "
                "WHERE pergunta_hash = ANY(:hashes) ORDER BY id"
            ),
            {"hashes": list(hashes)},
        ).all()

    agrupado: dict[str, list[PerguntaExemplo]] = {}
    for linha in linhas:
        agrupado.setdefault(linha.pergunta_hash, []).append(_de_linha(linha))
    return agrupado


def grupos(store=None) -> list[str]:
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        linhas = sessao.execute(
            text("SELECT DISTINCT grupo FROM exemplo_perguntas WHERE ativo ORDER BY grupo")
        ).all()
    return [linha.grupo for linha in linhas if linha.grupo]


# --- escrita ----------------------------------------------------------------


_INSERIR = text(
    """
    INSERT INTO exemplo_perguntas
        (grupo, pergunta, pergunta_hash, assunto, origem_esperada,
         origem_tambem_ok, criterio, ativo)
    VALUES
        (:grupo, :pergunta, :pergunta_hash, :assunto, :origem_esperada,
         CAST(:origem_tambem_ok AS TEXT[]), :criterio, TRUE)
    ON CONFLICT (grupo, pergunta_hash) DO UPDATE SET
        assunto = EXCLUDED.assunto,
        origem_esperada = EXCLUDED.origem_esperada,
        origem_tambem_ok = EXCLUDED.origem_tambem_ok,
        criterio = EXCLUDED.criterio,
        ativo = TRUE,
        atualizado_em = now()
    WHERE
        exemplo_perguntas.assunto IS DISTINCT FROM EXCLUDED.assunto
        OR exemplo_perguntas.origem_esperada IS DISTINCT FROM EXCLUDED.origem_esperada
        OR exemplo_perguntas.origem_tambem_ok IS DISTINCT FROM EXCLUDED.origem_tambem_ok
        OR exemplo_perguntas.criterio IS DISTINCT FROM EXCLUDED.criterio
        OR NOT exemplo_perguntas.ativo
    RETURNING id, (xmax = 0) AS inserido
    """
)


def _params(item: dict) -> dict:
    pergunta = str(item["pergunta"]).strip()
    origem = item["origem_esperada"]
    _validar(pergunta, origem)
    return {
        "grupo": (item.get("grupo") or "").strip(),
        "pergunta": pergunta,
        "pergunta_hash": telemetry.hash_pergunta(pergunta),
        "assunto": item.get("assunto") or None,
        "origem_esperada": origem,
        "origem_tambem_ok": _normalizar_tambem_ok(item.get("origem_tambem_ok")),
        "criterio": item.get("criterio") or None,
    }


@dataclass(frozen=True)
class ResumoUpsert:
    """O que um `upsert_muitos` fez. `inalterados` é o número que prova a
    idempotência: rodar o seed duas vezes deixa tudo nessa coluna."""

    inseridos: int = 0
    atualizados: int = 0
    inalterados: int = 0

    @property
    def total(self) -> int:
        return self.inseridos + self.atualizados + self.inalterados


def upsert_muitos(itens: list[dict], store=None) -> ResumoUpsert:
    """UPSERT idempotente do dataset inteiro, numa transação só.

    O `WHERE` do `DO UPDATE` é o que torna a 2ª execução um no-op de verdade:
    sem ele o UPDATE dispararia para toda linha, mexendo em `atualizado_em` e
    fazendo o resumo dizer "50 atualizados" numa rodada em que nada mudou.
    `xmax = 0` distingue INSERT de UPDATE no `RETURNING`; a linha que não volta
    é a que o `WHERE` filtrou — a inalterada.

    Transação única (um `commit` no fim) porque um seed pela metade é pior que
    um seed que falhou: metade do dataset novo com metade do antigo produz uma
    rodada que ninguém consegue interpretar.
    """
    if not itens:
        return ResumoUpsert()
    _ensure_table(store)

    inseridos = atualizados = 0
    with (store or get_vector_store()).session_maker() as sessao:
        for item in itens:
            linha = sessao.execute(_INSERIR, _params(item)).first()
            if linha is None:
                continue
            if linha.inserido:
                inseridos += 1
            else:
                atualizados += 1
        sessao.commit()

    return ResumoUpsert(
        inseridos=inseridos,
        atualizados=atualizados,
        inalterados=len(itens) - inseridos - atualizados,
    )


def criar(item: dict, store=None) -> PerguntaExemplo:
    """Cria — ou reativa/atualiza, se a chave natural já existir.

    Reaproveita o `_INSERIR` do seed de propósito: um POST com uma pergunta que
    já está lá (desativada, por exemplo) devolveria 409 e obrigaria quem
    integra a descobrir o `id` antes de tentar de novo. Aqui o efeito é o que
    ele queria — a pergunta passa a existir e a estar ativa.
    """
    params = _params(item)
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        sessao.execute(_INSERIR, params)
        sessao.commit()
        linha = sessao.execute(
            text(
                f"SELECT {_COLUNAS} FROM exemplo_perguntas "
                "WHERE grupo = :grupo AND pergunta_hash = :pergunta_hash"
            ),
            {"grupo": params["grupo"], "pergunta_hash": params["pergunta_hash"]},
        ).first()
    return _de_linha(linha)


# Só o que faz sentido editar. `pergunta_hash` fica de fora e é recalculado
# quando `pergunta` muda: deixar quem chama informar o hash abre caminho para um
# par (texto, hash) inconsistente, e o hash é exatamente a chave que liga esta
# linha à telemetria.
_EDITAVEIS = (
    "grupo",
    "pergunta",
    "assunto",
    "origem_esperada",
    "origem_tambem_ok",
    "criterio",
    "ativo",
)


def atualizar(id_: int, campos: dict, store=None) -> PerguntaExemplo | None:
    """PATCH parcial: só o que veio em `campos` é tocado."""
    mudancas = {k: v for k, v in campos.items() if k in _EDITAVEIS}
    if not mudancas:
        return obter(id_, store)

    atual = obter(id_, store)
    if atual is None:
        return None

    if "origem_esperada" in mudancas or "pergunta" in mudancas:
        _validar(
            str(mudancas.get("pergunta", atual.pergunta)).strip(),
            mudancas.get("origem_esperada", atual.origem_esperada),
        )
    if "origem_tambem_ok" in mudancas:
        mudancas["origem_tambem_ok"] = _normalizar_tambem_ok(mudancas["origem_tambem_ok"])

    atribuicoes: list[str] = []
    params: dict = {"id": id_}
    for campo, valor in mudancas.items():
        if campo == "origem_tambem_ok":
            atribuicoes.append("origem_tambem_ok = CAST(:origem_tambem_ok AS TEXT[])")
        elif campo == "pergunta":
            valor = str(valor).strip()
            atribuicoes.append("pergunta = :pergunta")
            atribuicoes.append("pergunta_hash = :pergunta_hash")
            params["pergunta_hash"] = telemetry.hash_pergunta(valor)
        else:
            atribuicoes.append(f"{campo} = :{campo}")
        params[campo] = valor
    atribuicoes.append("atualizado_em = now()")

    with (store or get_vector_store()).session_maker() as sessao:
        linha = sessao.execute(
            text(
                f"UPDATE exemplo_perguntas SET {', '.join(atribuicoes)} "
                f"WHERE id = :id RETURNING {_COLUNAS}"
            ),
            params,
        ).first()
        sessao.commit()
    return _de_linha(linha) if linha else None


def desativar(id_: int, store=None) -> bool:
    """DELETE lógico: `ativo = false`.

    Físico destruiria a comparabilidade histórica. Uma rodada de agosto tem
    linhas de `telemetria` com o `pergunta_hash` desta pergunta; apagar a linha
    deixaria esses registros órfãos, e o relatório de uma rodada antiga passaria
    a mostrar hash cru no lugar do texto — perda silenciosa, meses depois, de um
    dado que ninguém pensou em fazer backup. `ativo=false` tira a pergunta das
    PRÓXIMAS rodadas (é o que se quer dizer com "excluir") e mantém a leitura
    das passadas. Reativar é um PATCH com `ativo=true`.
    """
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        resultado = sessao.execute(
            text(
                "UPDATE exemplo_perguntas SET ativo = FALSE, atualizado_em = now() "
                "WHERE id = :id AND ativo"
            ),
            {"id": id_},
        )
        sessao.commit()
    return bool(resultado.rowcount)


def limpar(store=None) -> int:
    """Apaga o dataset inteiro. Só para a CLI de limpeza / base de teste zerada."""
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        resultado = sessao.execute(text("DELETE FROM exemplo_perguntas"))
        sessao.commit()
        return resultado.rowcount or 0
