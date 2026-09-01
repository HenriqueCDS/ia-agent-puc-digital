"""CLI para remover arquivos já indexados do pgvector.

    python -m scripts.remove_ingested guia-canvas.pdf        # por trecho do caminho
    python -m scripts.remove_ingested --assunto puc-digital  # só os ARQUIVOS da pasta
    python -m scripts.remove_ingested guia --yes              # sem confirmar
    python -m scripts.remove_ingested --web                   # TODO o conteúdo crawlado da allowlist
    python -m scripts.remove_ingested --web /biblioteca/      # só as páginas crawladas cuja URL casa

Mostra o que casou (com `scripts.list_ingested` como referência) e pede
confirmação antes de apagar — a menos que `--yes` seja passado. Sem isso, um
termo genérico demais (ex.: ".pdf") apagaria mais do que o esperado.

`--web` existe porque `--assunto` de propósito NÃO toca no conteúdo crawlado
(as páginas da `WEB_ALLOWLIST` têm o mesmo `assunto` dos PDFs — ver
`vector_store.delete_by_assunto`), então sem ele não havia como limpar o crawl
inteiro de uma vez.
"""

import typer

from app.db.vector_store import (
    delete_by_assunto,
    delete_by_source,
    get_vector_store,
    list_ingested_sources,
    list_web_sources,
)

app = typer.Typer(add_completion=False, help="Remove arquivos indexados do pgvector.")


@app.command()
def main(
    termo: str = typer.Argument(
        None, help="Trecho do caminho do arquivo (case-insensitive). Ex: guia-canvas.pdf"
    ),
    assunto: str = typer.Option(
        None,
        "--assunto",
        "-a",
        help="Remove os ARQUIVOS da pasta/assunto inteira (não o conteúdo crawlado da web; "
        "para esse, use --web).",
    ),
    web: bool = typer.Option(
        False,
        "--web",
        help="Age sobre o conteúdo crawlado da WEB_ALLOWLIST (source_type='web'). Sozinho, "
        "remove TUDO; com um termo, filtra por trecho da URL (ex.: 'puc-campinas.edu.br' ou '/biblioteca/').",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação."),
) -> None:
    if not termo and not assunto and not web:
        typer.secho(
            "Informe um termo (arquivo), --assunto (pasta) ou --web (conteúdo crawlado).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if assunto and (termo or web):
        typer.secho(
            "Use --assunto sozinho: termo busca por arquivo, --web busca por página crawlada.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    store = get_vector_store()

    if web:
        rows = list_web_sources(store)
        if termo:
            rows = [(p, c) for p, c in rows if termo.lower() in p.lower()]
        if not rows:
            alvo = f'que casem "{termo}"' if termo else "indexadas"
            typer.secho(f"Nenhuma página crawlada {alvo}.", fg=typer.colors.YELLOW)
            raise typer.Exit()
        _confirmar_e_remover(
            rows,
            resumo=f'web ~ "{termo}"' if termo else "web (todo o crawl)",
            yes=yes,
            remover=lambda: sum(delete_by_source(store, p) for p, _ in rows),
            rotulo="Páginas",
        )
        return

    if assunto:
        rows = [(p, c) for p, c in list_ingested_sources(store) if _assunto_de(p) == assunto]
        _confirmar_e_remover(
            rows,
            resumo=f'assunto "{assunto}"',
            yes=yes,
            remover=lambda: delete_by_assunto(store, assunto),
        )
        return

    casados = [(p, c) for p, c in list_ingested_sources(store) if termo.lower() in p.lower()]
    if not casados:
        typer.secho(f'Nenhum arquivo indexado casa com "{termo}".', fg=typer.colors.YELLOW)
        raise typer.Exit()

    def _remover_casados() -> int:
        return sum(delete_by_source(store, source_path) for source_path, _ in casados)

    _confirmar_e_remover(casados, resumo=f'"{termo}"', yes=yes, remover=_remover_casados)


def _assunto_de(source_path: str) -> str | None:
    """`source_path` é `.../data/raw/<assunto>/arquivo.pdf` — extrai o assunto
    sem precisar de outra coluna: é o mesmo valor que `pipeline._enrich` grava."""
    partes = source_path.replace("\\", "/").split("/")
    try:
        return partes[partes.index("raw") + 1]
    except (ValueError, IndexError):
        return None


def _confirmar_e_remover(rows, *, resumo: str, yes: bool, remover, rotulo: str = "Arquivos") -> None:
    total_chunks = sum(c for _, c in rows)
    typer.echo(f"{rotulo} que serão removidos ({resumo}):")
    for source_path, chunks in rows:
        typer.echo(f"  {chunks:>4}  {source_path}")
    typer.echo(f"\n{len(rows)} {rotulo.lower()}, {total_chunks} chunk(s) no total.")

    if not rows:
        raise typer.Exit()

    if not yes and not typer.confirm("Confirma a remoção?"):
        typer.secho("Cancelado.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    removidos = remover()
    typer.secho(f"{removidos} chunk(s) removido(s).", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
