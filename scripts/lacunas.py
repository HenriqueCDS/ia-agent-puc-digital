"""Relatório de lacunas (T3.3): o que os alunos perguntam e a base não responde.

    python -m scripts.lacunas
    python -m scripts.lacunas --dias 7 --limite 30
    python -m scripts.lacunas --json > lacunas.json

Este é o item de maior valor de produto do backlog, e a razão é simples: cada
resposta com `grounded=false` é um documento que falta indexar, e até T3.2 esse
sinal era descartado a cada request. O relatório transforma a telemetria já
gravada no roadmap de ingestão — priorizado por quantas vezes a pergunta
apareceu, não por palpite.

JANELA vs. RETENÇÃO — a tabela `telemetria` guarda `TELEMETRY_RETENTION_DAYS`
dias (7 por padrão, ver app/db/telemetry_store.py). Pedir `--dias` acima disso
não devolve mais dado, só uma janela vazia na ponta; o comando avisa quando isso
acontece em vez de deixar parecer que a semana foi tranquila.
"""

import json

import typer

from app.core.config import settings
from app.db.telemetry_store import consultar_lacunas, contar_perguntas

app = typer.Typer(add_completion=False, help="Perguntas que a base não respondeu, por frequência.")

def _situacao(lacuna) -> tuple[str, str]:
    """Rótulo e cor da linha. Cada caso exige uma ação diferente de quem cuida
    do conteúdo, e é por isso que eles não são só uma cor:

    - alguma ocorrência SEM resposta: o aluno foi para a secretaria. É o caso
      grave, e por isso ordena o relatório;
    - todas cobertas pela busca externa: a resposta existe numa página oficial.
      Indexar aquele conteúdo troca ~4s de busca externa por ~200ms de RAG —
      é a lacuna mais barata de fechar, porque a fonte já está identificada.
    """
    if lacuna.sem_resposta == lacuna.ocorrencias:
        return "sem resposta", typer.colors.RED
    if lacuna.sem_resposta:
        return f"{lacuna.sem_resposta}/{lacuna.ocorrencias} sem resposta", typer.colors.RED
    return "coberta pela web", typer.colors.YELLOW


@app.command()
def main(
    dias: int = typer.Option(7, help="Janela de análise, em dias."),
    limite: int = typer.Option(20, help="Quantas lacunas mostrar."),
    formato_json: bool = typer.Option(False, "--json", help="Saída JSON, para pipeline."),
) -> None:
    if dias > settings.telemetry_retention_days and not formato_json:
        typer.secho(
            f"aviso: a retenção da telemetria é de {settings.telemetry_retention_days} "
            f"dias; os dias além disso vêm vazios.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    lacunas = consultar_lacunas(dias=dias, limite=limite)
    total, total_lacunas = contar_perguntas(dias)

    if formato_json:
        typer.echo(json.dumps(
            {
                "dias": dias,
                "perguntas": total,
                "lacunas": total_lacunas,
                "itens": [
                    {
                        "tema": lacuna.rotulo,
                        "assuntos": lacuna.assuntos,
                        "ocorrencias": lacuna.ocorrencias,
                        "perguntas_distintas": lacuna.perguntas_distintas,
                        "sem_resposta": lacuna.sem_resposta,
                        "ultima_vez": lacuna.ultima_vez.isoformat(),
                    }
                    for lacuna in lacunas
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return

    if not total:
        typer.secho(
            f"Nenhuma pergunta registrada nos últimos {dias} dia(s).\n"
            "Confira TELEMETRY_DB_ENABLED=true no .env.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit()

    proporcao = 100 * total_lacunas / total
    typer.secho(f"\nÚltimos {dias} dia(s): {total} pergunta(s) registrada(s).", bold=True)
    typer.secho(
        f"{total_lacunas} ({proporcao:.0f}%) não foram respondidas pela base indexada.",
        fg=typer.colors.YELLOW if proporcao >= 20 else typer.colors.GREEN,
    )

    if not lacunas:
        typer.secho("\nNenhuma lacuna na janela — a base cobriu tudo que foi perguntado.",
                    fg=typer.colors.GREEN)
        raise typer.Exit()

    typer.echo("\n    n  dist  assunto        situação          tema")
    typer.echo("  " + "-" * 84)
    for lacuna in lacunas:
        situacao, cor = _situacao(lacuna)
        typer.echo(
            f"  {lacuna.ocorrencias:>3}  {lacuna.perguntas_distintas:>4}  "
            f"{lacuna.assuntos[:14]:<14} ",
            nl=False,
        )
        typer.secho(f"{situacao:<17} ", fg=cor, nl=False)
        typer.echo(lacuna.rotulo)

    typer.secho(
        "\nComo ler: cada linha é um tema que a base não cobre. A ordem é a de prioridade —\n"
        "primeiro o que ficou sem resposta nenhuma (o aluno foi para a secretaria), depois\n"
        "o que a busca externa cobriu, que é lacuna mais barata: a fonte já está achada.\n"
        "`dist` é quantas formulações diferentes chegaram ao mesmo tema — 1 pode ser um\n"
        "aluno insistindo, várias indicam que o tema é procurado de fato.\n"
        "Linhas `hash:...` não passaram pelo LLM (nada foi achado em nenhuma fonte), então\n"
        "não há tema legível — só a prova de que aquela pergunta se repete.",
        fg=typer.colors.BRIGHT_BLACK,
    )


if __name__ == "__main__":
    app()
