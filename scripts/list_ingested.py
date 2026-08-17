"""CLI para listar o que está indexado no banco.

    python -m scripts.list_ingested
"""

import typer

from app.db.vector_store import get_vector_store, list_ingested_sources

app = typer.Typer(add_completion=False, help="Lista os arquivos indexados na coleção atual.")


@app.command()
def main() -> None:
    store = get_vector_store()
    rows = list_ingested_sources(store)

    if not rows:
        typer.secho("Nenhum arquivo indexado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    total_chunks = 0
    for source_path, chunks in rows:
        typer.echo(f"{chunks:>4}  {source_path}")
        total_chunks += chunks

    typer.secho(
        f"\n{len(rows)} arquivo(s), {total_chunks} chunk(s) no total.",
        fg=typer.colors.GREEN,
    )


if __name__ == "__main__":
    app()
