"""CLI de perguntas — jeito mais rápido de avaliar a qualidade do RAG.

    python -m scripts.ask "Como envio uma atividade no Canvas?"
    python -m scripts.ask "Como acesso o portal?" --assunto puc-digital
    python -m scripts.ask "Como acesso o portal?" --debug   # mostra os chunks e scores
"""

import typer

from app.agent.responder import answer
from app.core.models import Query

app = typer.Typer(add_completion=False, help="Faz uma pergunta ao agente.")


@app.command()
def main(
    pergunta: str = typer.Argument(...),
    assunto: str | None = typer.Option(None, "--assunto", "-a", help="Filtra por assunto."),
    debug: bool = typer.Option(False, "--debug", "-d", help="Mostra os chunks recuperados."),
) -> None:
    resultado = answer(Query(text=pergunta, assunto=assunto))

    if debug:
        typer.secho("\n--- chunks recuperados ---", fg=typer.colors.CYAN)
        for chunk in resultado.sources:
            trecho = chunk.document.page_content[:200].replace("\n", " ")
            typer.echo(f"[{chunk.score:.3f}] {chunk.citation}\n      {trecho}...")
        typer.secho("--- fim ---\n", fg=typer.colors.CYAN)

    typer.echo(resultado.text)

    if resultado.origem == "web":
        typer.secho(
            "\n(respondido por busca em páginas públicas oficiais — não estava "
            "na base indexada; talvez falte documento)",
            fg=typer.colors.YELLOW,
        )
    elif not resultado.grounded:
        typer.secho(
            "\n(nada acima do limiar de relevância — talvez falte documento na base)",
            fg=typer.colors.YELLOW,
        )


if __name__ == "__main__":
    app()
