"""Lista os modelos que as SUAS chaves de API conseguem usar, por provider.

    python -m scripts.modelos                 # todos os providers da cadeia
    python -m scripts.modelos --provider groq # só um
    python -m scripts.modelos --filtro llama  # só os que contêm o termo

Existe por causa de um modo de falha específico. Quando um modelo sai do
catálogo (ou a sua conta não tem acesso a ele), o provedor responde:

    404 - The model `llama-3.3-70b-versatile` does not exist
          or you do not have access to it

Repare no "OU": a mensagem não distingue "esse modelo não existe mais" de "sua
chave não tem acesso" — e as duas se resolvem com o mesmo dado, o catálogo da
própria chave. Sem isto, o próximo passo é adivinhar nomes de modelo e reiniciar
o serviço a cada tentativa.

O que está configurado no `.env` aparece marcado com `<- .env`, então a linha
que falta é visível de imediato: modelo configurado que NÃO aparece na lista do
provider é exatamente o 404 acima.
"""

import typer

from app.core.config import settings
from app.providers.chain import modelo_padrao, provider_avulso, providers_conhecidos, sem_segredo

app = typer.Typer(add_completion=False, help="Lista os modelos disponíveis por provider.")


def _listar(nome: str, filtro: str) -> None:
    if nome not in providers_conhecidos():
        typer.secho(
            f"{nome}: provider desconhecido (válidos: {', '.join(providers_conhecidos())})",
            fg=typer.colors.RED,
        )
        return

    provider = provider_avulso(nome)
    if provider is None:
        typer.secho(f"\n{nome}: sem chave de API configurada", fg=typer.colors.YELLOW)
        return

    typer.secho(f"\n{nome}", fg=typer.colors.CYAN, bold=True)
    try:
        modelos = provider.listar_modelos()
    except NotImplementedError:
        typer.echo("  (este provider não expõe catálogo de modelos)")
        return
    except Exception as exc:
        # `sem_segredo` mesmo aqui: mensagem de erro de SDK ecoa credencial com
        # frequência, e a saída de uma CLI vai parar em ticket e print de tela.
        typer.secho(f"  falhou: {sem_segredo(str(exc))}", fg=typer.colors.RED)
        return

    atual = modelo_padrao(nome)
    visiveis = [m for m in modelos if filtro.casefold() in m.casefold()]
    for modelo in visiveis:
        marca = "  <- .env" if modelo == atual else ""
        typer.secho(f"  {modelo}{marca}", fg=typer.colors.GREEN if marca else None)

    if filtro and not visiveis:
        typer.echo(f"  (nenhum dos {len(modelos)} modelos contém {filtro!r})")

    # O ponto inteiro da CLI: o modelo do `.env` que o provider não conhece é a
    # causa do 404 em produção, e é a única linha que o operador precisa ver.
    if atual and atual not in modelos:
        typer.secho(
            f"  ATENÇÃO: {atual!r} está no .env mas NÃO aparece no catálogo desta chave "
            "— é este o 404 que a cadeia registra em ERROR.",
            fg=typer.colors.RED,
            bold=True,
        )


@app.command()
def main(
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="Só este provider (gemini, groq, openrouter)."
    ),
    filtro: str = typer.Option("", "--filtro", "-f", help="Só modelos que contêm o termo."),
) -> None:
    nomes = [provider] if provider else settings.llm_providers_lista
    if not nomes:
        typer.secho("LLM_PROVIDERS está vazio no .env.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    for nome in nomes:
        _listar(nome.strip().casefold(), filtro)

    typer.echo("\nPara usar um destes: ajuste o .env (CHAT_MODEL/GROQ_MODEL/OPENROUTER_MODEL)")
    typer.echo("ou teste sem reiniciar: python -m scripts.ask \"...\" --modelo groq:<modelo>")


if __name__ == "__main__":
    app()
