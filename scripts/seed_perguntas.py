"""Carrega `eval/perguntas/perguntas.jsonc` na tabela `exemplo_perguntas`.

    python -m scripts.seed_perguntas               # UPSERT a partir do JSONC
    python -m scripts.seed_perguntas --dry-run     # só mostra o que entraria
    python -m scripts.seed_perguntas --arquivo outro.jsonc

O JSONC continua versionado como SEMENTE — é o que entra num checkout novo e o
que tem histórico revisável em diff. Depois do primeiro seed, a fonte VIVA é o
banco: edições feitas pela tela de revisão ou por `/v1/perguntas` ficam lá, e
um novo seed do arquivo NÃO as apaga (só faz UPSERT do que o arquivo traz).

IDEMPOTÊNCIA — a chave natural é `(grupo, pergunta_hash)` (ver
`app/db/perguntas_store.py`: o dataset repete perguntas entre grupos de
propósito). O UPSERT só toca uma linha se algum campo mudou; rodar duas vezes
seguidas deixa `inseridos=0, atualizados=0` e não mexe em `atualizado_em`.

O que este script NÃO faz: remover do banco uma pergunta que saiu do arquivo.
Isso é intencional — o arquivo é semente, não espelho. Para tirar uma pergunta
das próximas rodadas, use `DELETE`/`ativo=false` via `/v1/perguntas` (delete
lógico, ver a docstring de `perguntas_store.desativar`).
"""

from pathlib import Path

import typer

from app.db import perguntas_store
from scripts.eval_run import _carregar_dataset

app = typer.Typer(add_completion=False, help="Semeia exemplo_perguntas a partir do JSONC.")

_PADRAO = Path("eval/perguntas/perguntas.jsonc")


@app.command()
def main(
    arquivo: Path = typer.Option(_PADRAO, "--arquivo", "-a", help="Dataset JSONC de origem."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Não escreve: lista o que entraria e sai."
    ),
) -> None:
    if not arquivo.exists():
        raise typer.BadParameter(f"{arquivo} não existe.")

    itens = _carregar_dataset(arquivo)  # reaproveita a remoção de comentários `//`
    typer.secho(f"{len(itens)} item(ns) lidos de {arquivo}", fg=typer.colors.CYAN)

    grupos: dict[str, int] = {}
    for item in itens:
        grupos[item.get("grupo") or "(sem grupo)"] = grupos.get(item.get("grupo") or "(sem grupo)", 0) + 1
    for grupo, n in grupos.items():
        typer.echo(f"  {grupo:<12} {n:>3}")

    if dry_run:
        # Valida cada item pelo mesmo caminho do UPSERT, sem tocar no banco.
        for i, item in enumerate(itens):
            try:
                perguntas_store._params(item)
            except (KeyError, ValueError) as exc:
                typer.secho(f"  item {i}: {exc}", fg=typer.colors.RED)
        typer.secho("\n--dry-run: nada foi escrito.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    resumo = perguntas_store.upsert_muitos(itens)
    cor = typer.colors.GREEN if resumo.atualizados or resumo.inseridos else typer.colors.CYAN
    typer.secho(
        f"\ninseridos={resumo.inseridos}  atualizados={resumo.atualizados}  "
        f"inalterados={resumo.inalterados}  (total {resumo.total})",
        fg=cor,
        bold=True,
    )
    if resumo.inseridos == 0 and resumo.atualizados == 0:
        typer.secho("banco já estava em dia com o arquivo.", fg=typer.colors.CYAN)


if __name__ == "__main__":
    app()
