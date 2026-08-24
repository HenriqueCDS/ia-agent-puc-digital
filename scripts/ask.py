"""CLI de perguntas — jeito mais rápido de avaliar a qualidade do RAG.

    python -m scripts.ask "Como envio uma atividade no Canvas?"
    python -m scripts.ask "Como acesso o portal?" --assunto puc-digital
    python -m scripts.ask "Como acesso o portal?" --debug   # mostra os chunks e scores
    python -m scripts.ask "Como envio a atividade?" --modelo gemini:gemini-3.6-flash

`--modelo` responde com UM modelo específico, sem cadeia de fallback — é o
jeito de comparar dois modelos na base real sem editar o `.env`. Por padrão
não usa cache (repetir a mesma pergunta com o mesmo modelo chama o LLM de
novo); ligue `MODELO_OVERRIDE_CACHE_ENABLED` no `.env` para mudar isso — o
modelo entra na chave do cache, então nunca mistura com outro modelo nem com
a cadeia normal. Ver os nomes válidos com `python -m scripts.modelos`.

A telemetria (1 linha JSON por pergunta) sai em stderr, separada da resposta, e
vai também para a tabela `telemetria` no Postgres (retenção de 7 dias):

    python -m scripts.ask "..." 2>> telemetria.jsonl
"""

import typer

from app.agent.responder import answer
from app.core import telemetry
from app.core.models import Query
from app.db import telemetry_store

app = typer.Typer(add_completion=False, help="Faz uma pergunta ao agente.")


@app.command()
def main(
    pergunta: str = typer.Argument(...),
    assunto: str | None = typer.Option(None, "--assunto", "-a", help="Filtra por assunto."),
    debug: bool = typer.Option(False, "--debug", "-d", help="Mostra os chunks recuperados."),
    modelo: str | None = typer.Option(
        None,
        "--modelo",
        "-m",
        help="Responde com este modelo (`[provider:]modelo`), sem fallback.",
    ),
) -> None:
    telemetry.configurar_logs()
    telemetry_store.habilitar()
    telemetry.set_canal("cli")

    resultado = answer(Query(text=pergunta, assunto=assunto, modelo=modelo))

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
