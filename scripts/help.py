"""Lista todas as CLIs do projeto e uma breve descrição de cada uma.

    python -m scripts.help

A lista é estática (não importa os outros módulos) de propósito: alguns deles
carregam o modelo de embeddings na importação, o que tornaria `--help` lento
só para descobrir o que existe. Ao adicionar uma CLI nova em `scripts/`,
inclua uma linha aqui — a descrição deve bater com o `help=` do `typer.Typer`
do próprio módulo.
"""

import typer

app = typer.Typer(add_completion=False, help="Lista as CLIs do projeto.")

# (comando, descrição) — mesma descrição do `help=` do typer.Typer de cada módulo.
_COMANDOS = [
    ("python -m scripts.ask", "Faz uma pergunta ao agente."),
    ("python -m scripts.modelos", "Lista os modelos disponíveis por provider."),
    ("python -m scripts.ingest", "Indexa os arquivos de data/raw/<assunto>/."),
    ("python -m scripts.list_ingested", "Lista os arquivos indexados na coleção atual."),
    ("python -m scripts.remove_ingested", "Remove arquivos indexados do pgvector."),
    ("python -m scripts.lacunas", "Perguntas que a base não respondeu, por frequência."),
    ("python -m scripts.clear_cache", "Apaga o cache de respostas (resposta_cache)."),
    ("python -m scripts.clear_logs", "Apaga a telemetria/logs (tabela telemetria)."),
    ("python -m scripts.eval_run", "Roda o dataset de avaliação contra o agente."),
    ("python -m scripts.eval_report", "Compara origem esperada x obtida via telemetria."),
]


@app.command()
def main() -> None:
    largura = max(len(comando) for comando, _ in _COMANDOS)
    for comando, descricao in _COMANDOS:
        typer.secho(comando.ljust(largura), fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {descricao}")
    typer.echo(f"\nUse 'python -m scripts.<comando> --help' para as opções de cada uma.")


if __name__ == "__main__":
    app()
