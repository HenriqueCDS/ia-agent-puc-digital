"""CLI para apagar os caches de resposta (`resposta_cache` e
`resposta_cache_pergunta`).

    python -m scripts.clear_cache          # pede confirmação
    python -m scripts.clear_cache --yes    # sem confirmar

Útil para deixar a base limpa antes de rodar testes: sem isso, uma resposta
já cacheada de uma execução anterior mascararia uma mudança no prompt/modelo.
Apaga os DOIS caches — o pós-retrieval (por conjunto de chunks) e o
pré-retrieval (por pergunta + assunto, ver app/db/pre_retrieval_cache.py).
"""

import typer

from app.db.pre_retrieval_cache import clear_pre_retrieval_cache
from app.db.response_cache import clear_cache

app = typer.Typer(add_completion=False, help="Apaga os caches de resposta.")


@app.command()
def main(
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
) -> None:
    if not yes and not typer.confirm("Apagar TODO o cache de respostas?"):
        typer.secho("Cancelado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    removidos = clear_cache()
    pre_removidos = clear_pre_retrieval_cache()
    typer.secho(
        f"{removidos} entrada(s) pós-retrieval e {pre_removidos} pré-retrieval removida(s).",
        fg=typer.colors.GREEN,
    )


if __name__ == "__main__":
    app()
