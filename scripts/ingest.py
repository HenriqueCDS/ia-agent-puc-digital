"""CLI de ingestão.

    python -m scripts.ingest canvas
    python -m scripts.ingest canvas puc-digital
"""

import logging

import typer

from app.ingestion.pipeline import ingest_assunto

app = typer.Typer(add_completion=False, help="Indexa os arquivos de data/raw/<assunto>/.")


@app.command()
def main(
    assuntos: list[str] = typer.Argument(..., help="Pastas em data/raw/ (ex: canvas)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Mostra arquivo a arquivo."),
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
    )

    for assunto in assuntos:
        try:
            report = ingest_assunto(assunto)
        except FileNotFoundError as exc:
            typer.secho(f"[{assunto}] {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        typer.secho(
            f"[{assunto}] {report.arquivos} arquivo(s), {report.chunks} chunk(s) indexado(s).",
            fg=typer.colors.GREEN,
        )
        if report.ignorados:
            typer.secho(
                f"  ignorados (extensão não suportada): {', '.join(report.ignorados)}",
                fg=typer.colors.YELLOW,
            )


if __name__ == "__main__":
    app()
