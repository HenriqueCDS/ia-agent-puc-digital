"""CLI para remover arquivos já indexados do pgvector.

    python -m scripts.remove_ingested guia-canvas.pdf        # por trecho do caminho
    python -m scripts.remove_ingested --assunto puc-digital  # só os ARQUIVOS da pasta
    python -m scripts.remove_ingested guia --yes              # sem confirmar

Mostra o que casou (com `scripts.list_ingested` como referência) e pede
confirmação antes de apagar — a menos que `--yes` seja passado. Sem isso, um
termo genérico demais (ex.: ".pdf") apagaria mais do que o esperado.
"""

import typer

from app.db.vector_store import delete_by_assunto, delete_by_source, get_vector_store, list_ingested_sources

app = typer.Typer(add_completion=False, help="Remove arquivos indexados do pgvector.")


@app.command()
def main(
    termo: str = typer.Argument(
        None, help="Trecho do caminho do arquivo (case-insensitive). Ex: guia-canvas.pdf"
    ),
    assunto: str = typer.Option(
        None,
        "--assunto",
        "-a",
        help="Remove os ARQUIVOS da pasta/assunto inteira (não o conteúdo crawlado da web; "
        "para esse, passe um trecho da URL como termo).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
) -> None:
    if not termo and not assunto:
        typer.secho("Informe um termo (arquivo) ou --assunto (pasta inteira).", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if termo and assunto:
        typer.secho("Use um OU outro: termo busca por arquivo, --assunto apaga a pasta toda.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    store = get_vector_store()

    if assunto:
        rows = [(p, c) for p, c in list_ingested_sources(store) if _assunto_de(p) == assunto]
        _confirmar_e_remover(
            rows,
            resumo=f'assunto "{assunto}"',
            yes=yes,
            remover=lambda: delete_by_assunto(store, assunto),
        )
        return

    casados = [(p, c) for p, c in list_ingested_sources(store) if termo.lower() in p.lower()]
    if not casados:
        typer.secho(f'Nenhum arquivo indexado casa com "{termo}".', fg=typer.colors.YELLOW)
        raise typer.Exit()

    def _remover_casados() -> int:
        return sum(delete_by_source(store, source_path) for source_path, _ in casados)

    _confirmar_e_remover(casados, resumo=f'"{termo}"', yes=yes, remover=_remover_casados)


def _assunto_de(source_path: str) -> str | None:
    """`source_path` é `.../data/raw/<assunto>/arquivo.pdf` — extrai o assunto
    sem precisar de outra coluna: é o mesmo valor que `pipeline._enrich` grava."""
    partes = source_path.replace("\\", "/").split("/")
    try:
        return partes[partes.index("raw") + 1]
    except (ValueError, IndexError):
        return None


def _confirmar_e_remover(rows, *, resumo: str, yes: bool, remover) -> None:
    total_chunks = sum(c for _, c in rows)
    typer.echo(f"Arquivos que serão removidos ({resumo}):")
    for source_path, chunks in rows:
        typer.echo(f"  {chunks:>4}  {source_path}")
    typer.echo(f"\n{len(rows)} arquivo(s), {total_chunks} chunk(s) no total.")

    if not rows:
        raise typer.Exit()

    if not yes and not typer.confirm("Confirma a remoção?"):
        typer.secho("Cancelado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    removidos = remover()
    typer.secho(f"{removidos} chunk(s) removido(s).", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
