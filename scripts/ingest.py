"""CLI de ingestão.

    python -m scripts.ingest canvas
    python -m scripts.ingest canvas puc-digital
    python -m scripts.ingest puc-digital --apenas-novos
"""

import logging

import typer

from app.ingestion.pipeline import ingest_assunto

app = typer.Typer(add_completion=False, help="Indexa os arquivos de data/raw/<assunto>/.")


@app.command()
def main(
    assuntos: list[str] = typer.Argument(..., help="Pastas em data/raw/ (ex: canvas)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Mostra arquivo a arquivo."),
    apenas_novos: bool = typer.Option(
        False,
        "--apenas-novos",
        "-n",
        help="Pula arquivos cujo caminho já está indexado (não reprocessa, não chama o embedder).",
    ),
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
    )

    for assunto in assuntos:
        try:
            report = ingest_assunto(assunto, apenas_novos=apenas_novos)
        except FileNotFoundError as exc:
            typer.secho(f"[{assunto}] {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        typer.secho(
            f"[{assunto}] {report.arquivos} arquivo(s), {report.chunks} chunk(s) indexado(s).",
            fg=typer.colors.GREEN,
        )
        if report.duplicados:
            typer.secho(
                f"  {report.duplicados} chunk(s) descartado(s) por já existir com o mesmo "
                "conteúdo em outra fonte",
                fg=typer.colors.YELLOW,
            )
        if report.pulados:
            typer.secho(
                f"  {report.pulados} arquivo(s) pulado(s) (já indexado(s))",
                fg=typer.colors.YELLOW,
            )
        if report.ignorados:
            typer.secho(
                f"  ignorados (extensão não suportada): {', '.join(report.ignorados)}",
                fg=typer.colors.YELLOW,
            )


if __name__ == "__main__":
    app()
