"""CLI de avaliação — audita, na tabela `telemetria`, o que o agente
respondeu de fato para o dataset de teste, e compara com a origem esperada.

    python -m scripts.eval_report                 # dataset: banco (exemplo_perguntas)
    python -m scripts.eval_report --grupo teste2
    python -m scripts.eval_report --detalhe
    python -m scripts.eval_report --fonte arquivo --json > relatorio.json

Complementa `scripts.eval_run`: aquele já devolve o resultado na hora (arquivo
local); este lê a MESMA fonte de verdade do `scripts.lacunas` — a telemetria
persistida no Postgres — o que permite comparar rodadas passadas (duas
janelas de `--dias`) sem reexecutar nada, e serve de checagem de que a
telemetria está gravando o que o agente de fato decidiu.

FONTE DO DATASET — como o `eval_run`, o default é `--fonte db` (a tabela
`exemplo_perguntas`). Ler do JSONC com `--fonte arquivo` compararia a origem
obtida contra uma `origem_esperada` que pode já ter sido corrigida pela tela
`/revisao` ou por `/v1/perguntas` — e o relatório apontaria "divergência" numa
pergunta que já foi reclassificada.

CORRELAÇÃO POR HASH — a telemetria nunca grava o texto da pergunta (só um
hash de 12 chars, ver `app/core/telemetry.py`): este script calcula o hash de
cada pergunta com a mesma função (`telemetry.hash_pergunta`) e casa com as
linhas gravadas com `canal='eval'`. Perguntas repetidas na janela usam a
ocorrência MAIS RECENTE (ver `telemetry_store.origem_por_hash`).

RESPOSTA E MODELO — a telemetria normalmente NUNCA grava o texto da resposta.
A exceção é só o canal `eval`: `responder.answer` grava `Registro.resposta`
quando `canal == "eval"`, porque o dataset de teste é sintético (nenhum aluno
real por trás). `--detalhe` mostra pergunta + resposta + esperado + obtido +
modelo de cada item; sem a flag, só o resumo por categoria.
"""

import json
import re
from pathlib import Path

import typer

from app.core import telemetry
from app.core.config import settings
from app.db.telemetry_store import origem_por_hash

app = typer.Typer(add_completion=False, help="Compara origem esperada x obtida via telemetria.")


def _carregar_dataset(caminho: Path) -> list[dict]:
    # Mesmo dataset do `scripts.eval_run` — JSONC, linhas `//` são comentário.
    texto = re.sub(r"^\s*//.*$", "", caminho.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return json.loads(texto)


def _carregar_itens(fonte: str, dataset: Path, grupo: str | None) -> list[dict]:
    """`--fonte db` (default) usa a tabela `exemplo_perguntas` — a mesma fonte
    viva do `eval_run`. Ler do JSONC quando o dataset já foi editado pela API /
    pela tela compararia contra uma expectativa desatualizada."""
    if fonte == "db":
        from app.db import perguntas_store

        return [p.como_item() for p in perguntas_store.listar(grupo=grupo, apenas_ativas=True)]
    itens = _carregar_dataset(dataset)
    return [i for i in itens if grupo is None or (i.get("grupo") or "") == grupo]


def _origens_aceitas(item: dict) -> list[str]:
    """`origem_esperada` + os aliases de `origem_tambem_ok` — igual `scripts.eval_run`.

    `nenhuma` e `encaminhado` dão a MESMA mensagem ao aluno; um dataset marca
    `origem_tambem_ok: ["nenhuma"]` nas perguntas em que o agente legitimamente
    não sabe, sem afrouxar os casos (PII/injeção) em que a distinção importa.
    """
    return [item["origem_esperada"], *item.get("origem_tambem_ok", ())]


def _resumir(resposta: str | None, limite: int = 300) -> str:
    if not resposta:
        return "(sem registro — resposta só é gravada por scripts.eval_run)"
    resposta = " ".join(resposta.split())
    return resposta if len(resposta) <= limite else resposta[:limite] + "..."


@app.command()
def main(
    dataset: Path = typer.Argument(
        Path("eval/perguntas/perguntas.jsonc"), help="Dataset JSONC — só com --fonte arquivo."
    ),
    fonte: str = typer.Option(
        "db", "--fonte", help="`db` (exemplo_perguntas, a fonte viva) ou `arquivo`."
    ),
    grupo: str | None = typer.Option(None, "--grupo", "-g", help="Só um bloco do dataset."),
    dias: int = typer.Option(1, help="Janela de busca na telemetria, em dias."),
    canal: str = typer.Option("eval", help="Canal gravado por scripts.eval_run."),
    detalhe: bool = typer.Option(
        False, "--detalhe", "-d", help="Lista pergunta/resposta/esperado/obtido/modelo de cada item."
    ),
    formato_json: bool = typer.Option(False, "--json", help="Saída JSON, para pipeline."),
) -> None:
    if fonte not in ("db", "arquivo"):
        raise typer.BadParameter("--fonte deve ser 'db' ou 'arquivo'.")

    retencao = (
        settings.telemetry_retention_days_eval if canal == "eval"
        else settings.telemetry_retention_days
    )
    if dias > retencao and not formato_json:
        typer.secho(
            f"aviso: a retenção do canal {canal!r} é de {retencao} dias; "
            "os dias além disso vêm vazios.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    itens = _carregar_itens(fonte, dataset, grupo)
    hashes = [telemetry.hash_pergunta(item["pergunta"]) for item in itens]
    registrado = origem_por_hash(hashes, dias=dias, canal=canal)

    linhas = []
    for item, h in zip(itens, hashes):
        achado = registrado.get(h)
        origem_obtida = achado.origem if achado else None
        linhas.append({
            "pergunta": item["pergunta"],
            "resposta": achado.resposta if achado else None,
            "origem_esperada": item["origem_esperada"],
            "origem_tambem_ok": item.get("origem_tambem_ok") or None,
            "origem_obtida": origem_obtida,
            "modelo": achado.modelo if achado else None,
            "acertou": origem_obtida in _origens_aceitas(item),
            "sem_registro": achado is None,
            # INF-7: nomes iguais aos de `scripts.eval_run` — são o que o
            # RETRIEVAL trouxe (quase sempre `top_k`), não as fontes da resposta.
            # `n_chunks` (nome do campo na telemetria/JSONB) chamado assim aqui
            # fazia os dois relatórios parecerem se contradizer.
            "score_top": achado.score_top if achado else None,
            "score_min": achado.score_min if achado else None,
            "chunks_recuperados": achado.n_chunks if achado else None,
            # RET-2: score_top − score_min. Feature de confiança para acumular a
            # mediana por item ao longo de N rodadas — não é critério de rota.
            "margem_relativa": achado.margem_relativa if achado else None,
        })

    if formato_json:
        typer.echo(json.dumps(
            {"dias": dias, "canal": canal, "itens": linhas},
            ensure_ascii=False,
            indent=2,
        ))
        return

    sem_registro = sum(1 for l in linhas if l["sem_registro"])
    if sem_registro:
        typer.secho(
            f"aviso: {sem_registro} pergunta(s) do dataset sem nenhuma linha na telemetria "
            f"nos últimos {dias} dia(s) (canal='{canal}') — rode scripts.eval_run antes.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    total = len(linhas)
    acertos = sum(1 for l in linhas if l["acertou"])
    typer.secho(f"\nGeral: {acertos}/{total} ({100 * acertos / total:.0f}%)", bold=True)

    typer.echo("\ncategoria     acertos  total   taxa")
    typer.echo("-" * 38)
    for categoria in ("base", "web", "encaminhado"):
        do_grupo = [l for l in linhas if l["origem_esperada"] == categoria]
        if not do_grupo:
            continue
        acertos_grupo = sum(1 for l in do_grupo if l["acertou"])
        taxa = 100 * acertos_grupo / len(do_grupo)
        cor = typer.colors.GREEN if taxa == 100 else (typer.colors.YELLOW if taxa >= 70 else typer.colors.RED)
        typer.echo(f"{categoria:<13} ", nl=False)
        typer.secho(f"{acertos_grupo:>5}/{len(do_grupo):<4}  {taxa:>4.0f}%", fg=cor)
    typer.echo("-" * 38)
    typer.echo(f"{'geral':<13} {acertos:>5}/{total:<4}  {100 * acertos / total:>4.0f}%")

    if detalhe:
        typer.secho("\nDetalhe:", bold=True)
        for l in linhas:
            cor = typer.colors.GREEN if l["acertou"] else typer.colors.RED
            esp = l["origem_esperada"]
            if l["origem_tambem_ok"]:
                esp += f"(+{'/'.join(l['origem_tambem_ok'])})"
            typer.secho(f"\n[{esp} -> {l['origem_obtida'] or '(sem registro)'}] ", fg=cor, nl=False)
            typer.echo(f"modelo={l['modelo'] or '—'}")
            margem = l["margem_relativa"]
            typer.echo(
                f"  score_top={l['score_top'] if l['score_top'] is not None else '—'}"
                f"  margem_relativa={margem if margem is not None else '—'}"
            )
            typer.echo(f"  P: {l['pergunta']}")
            typer.echo(f"  R: {_resumir(l['resposta'])}")

    divergencias = [l for l in linhas if not l["acertou"]]
    if divergencias:
        typer.secho("\nDivergências:", fg=typer.colors.RED)
        typer.echo("  esperado     obtido       modelo                  pergunta")
        for l in divergencias:
            obtido = l["origem_obtida"] or "(sem registro)"
            modelo = l["modelo"] or "—"
            typer.echo(f"  {l['origem_esperada']:<12} {obtido:<12} {modelo:<23} {l['pergunta']}")


if __name__ == "__main__":
    app()
