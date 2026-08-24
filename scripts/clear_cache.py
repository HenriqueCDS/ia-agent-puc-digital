"""CLI para apagar o cache de respostas (tabela `resposta_cache`).

    python -m scripts.clear_cache          # pede confirmação
    python -m scripts.clear_cache --yes    # sem confirmar

Útil para deixar a base limpa antes de rodar testes: sem isso, uma resposta
já cacheada de uma execução anterior mascararia uma mudança no prompt/modelo.
"""

import typer

from app.db.response_cache import clear_cache

app = typer.Typer(add_completion=False, help="Apaga o cache de respostas (resposta_cache).")


@app.command()
def main(
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
) -> None:
    if not yes and not typer.confirm("Apagar TODO o cache de respostas?"):
        typer.secho("Cancelado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    removidos = clear_cache()
    typer.secho(f"{removidos} entrada(s) de cache removida(s).", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
