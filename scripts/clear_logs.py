"""CLI para apagar a telemetria/logs (tabela `telemetria`).

    python -m scripts.clear_logs          # pede confirmação
    python -m scripts.clear_logs --yes    # sem confirmar

Útil para deixar a base limpa antes de rodar testes: sem isso, perguntas de
execuções anteriores continuariam contando no relatório de lacunas
(scripts.lacunas) e em qualquer métrica derivada da telemetria.
"""

import typer

from app.db.revisao_store import limpar as limpar_veredictos
from app.db.telemetry_store import limpar_telemetria

app = typer.Typer(add_completion=False, help="Apaga a telemetria/logs (tabela telemetria).")


@app.command()
def main(
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
) -> None:
    if not yes and not typer.confirm("Apagar TODA a telemetria/logs?"):
        typer.secho("Cancelado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    removidos = limpar_telemetria()
    typer.secho(f"{removidos} registro(s) de telemetria removido(s).", fg=typer.colors.GREEN)
    # Os veredictos da revisão referenciam `telemetria.id`; sem isto virariam
    # órfãos apontando para linhas que não existem mais.
    vered = limpar_veredictos()
    if vered:
        typer.secho(f"{vered} veredito(s) de revisão removido(s).", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
