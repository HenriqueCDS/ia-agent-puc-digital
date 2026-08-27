"""CLI de execução do dataset de avaliação — roda cada pergunta de um JSON
contra o agente de verdade e salva o resultado (origem esperada vs. obtida).

    python -m scripts.eval_run
    python -m scripts.eval_run eval/perguntas_teste.json
    python -m scripts.eval_run eval/perguntas_teste.json --saida eval/resultados/run1.csv --formato csv

    # rodada de calibração recomendada (ver eval/analise-telemetria-2026-08-27.md §10):
    python -m scripts.eval_run eval/perguntas_teste2.json -m huggingface:meta-llama/Llama-3.3-70B-Instruct -c --timeout 15

Existe para apoiar a calibração de `CHUNK_SIZE`/`RELEVANCE_THRESHOLD` (e,
com `--modelo`, comparação de modelo): rode o mesmo dataset antes e depois
de um ajuste no `.env`, reingira se `CHUNK_SIZE` mudou, e compare os dois
resultados. Este script dá o resultado na hora (arquivo local); para
auditar o que ficou gravado na telemetria (mesma fonte do `scripts.lacunas`),
use `scripts.eval_report` depois.

Flags que existem por causa de rate limit / cota do tier gratuito:

- `--modelo/-m` fixa UM modelo sem cadeia de fallback. Sem isso, a cadeia pode
  responder com um provider diferente a cada pergunta (o do topo estoura a
  cota no meio da rodada) e o resultado deixa de comparar a mesma coisa. Sem
  `-m`, o script avisa.
- `--limpar-cache/-c` apaga a `resposta_cache` antes de rodar — sem isso a
  rodada mede o cache, não o pipeline, e a única pergunta não-cacheada pode
  derrubar tudo num 413.
- `--timeout` sobrescreve `LLM_TIMEOUT` só nesta execução: com o provider do
  topo sem cota, cada pergunta queima o timeout inteiro antes do fallback —
  15s corta esse tempo morto pela metade.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from app.agent.responder import answer
from app.core import telemetry
from app.core.config import settings
from app.core.models import Query
from app.db import telemetry_store
from app.db.response_cache import clear_cache

app = typer.Typer(add_completion=False, help="Roda o dataset de avaliação contra o agente.")

_CANAL = "eval"
_CAMPOS_SAIDA = [
    "pergunta",
    "assunto",
    "origem_esperada",
    "origem_obtida",
    "acertou",
    "grounded",
    "cached",
    "score_top",
    "n_chunks",
]


def _carregar_dataset(caminho: Path) -> list[dict]:
    itens = json.loads(caminho.read_text(encoding="utf-8"))
    for i, item in enumerate(itens):
        faltando = {"pergunta", "origem_esperada"} - item.keys()
        if faltando:
            raise typer.BadParameter(f"item {i} do dataset sem campo(s) {faltando}: {item}")
    return itens


def _rodar(itens: list[dict], modelo: str | None) -> list[dict]:
    linhas = []
    for i, item in enumerate(itens, start=1):
        pergunta, assunto = item["pergunta"], item.get("assunto")
        typer.echo(f"[{i}/{len(itens)}] {pergunta[:70]}", err=True)

        resultado = answer(Query(text=pergunta, assunto=assunto, modelo=modelo))
        score_top = round(resultado.sources[0].score, 4) if resultado.sources else None

        linhas.append({
            "pergunta": pergunta,
            "assunto": assunto,
            "origem_esperada": item["origem_esperada"],
            "origem_obtida": resultado.origem,
            "acertou": resultado.origem == item["origem_esperada"],
            "grounded": resultado.grounded,
            "cached": resultado.cached,
            "score_top": score_top,
            "n_chunks": len(resultado.sources),
        })
    return linhas


def _salvar(linhas: list[dict], caminho: Path, formato: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if formato == "csv":
        with caminho.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CAMPOS_SAIDA)
            writer.writeheader()
            writer.writerows(linhas)
    else:
        caminho.write_text(json.dumps(linhas, ensure_ascii=False, indent=2), encoding="utf-8")


def _resumo(linhas: list[dict]) -> None:
    typer.echo("")
    total = len(linhas)
    acertos = sum(1 for l in linhas if l["acertou"])
    typer.secho(
        f"Geral: {acertos}/{total} ({100 * acertos / total:.0f}%)",
        fg=typer.colors.GREEN if acertos == total else typer.colors.YELLOW,
        bold=True,
    )

    for categoria in ("base", "web", "encaminhado"):
        do_grupo = [l for l in linhas if l["origem_esperada"] == categoria]
        if not do_grupo:
            continue
        acertos_grupo = sum(1 for l in do_grupo if l["acertou"])
        typer.echo(f"  {categoria:<12} {acertos_grupo:>2}/{len(do_grupo):<2}"
                    f" ({100 * acertos_grupo / len(do_grupo):.0f}%)")

    erros = [l for l in linhas if not l["acertou"]]
    if erros:
        typer.secho("\nDivergências:", fg=typer.colors.RED)
        for l in erros:
            typer.echo(f"  esperado={l['origem_esperada']:<11} obtido={l['origem_obtida']:<11} {l['pergunta']}")


@app.command()
def main(
    dataset: Path = typer.Argument(
        Path("eval/perguntas_teste.json"), help="JSON com pergunta/assunto/origem_esperada."
    ),
    saida: Path | None = typer.Option(
        None, "--saida", "-o", help="Onde salvar (default: eval/resultados/<timestamp>.json)."
    ),
    formato: str = typer.Option("json", "--formato", "-f", help="json ou csv."),
    modelo: str | None = typer.Option(
        None, "--modelo", "-m", help="Fixa um modelo (`[provider:]modelo`), sem fallback."
    ),
    limpar_cache: bool = typer.Option(
        False, "--limpar-cache", "-c",
        help="Apaga a resposta_cache antes de rodar (sem isso a rodada mede o cache).",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout",
        help="Sobrescreve LLM_TIMEOUT (s) só nesta rodada. Ex.: 15 corta pela metade "
             "o tempo morto quando o provider do topo está sem cota.",
    ),
) -> None:
    if formato not in ("json", "csv"):
        raise typer.BadParameter("--formato deve ser 'json' ou 'csv'.")

    itens = _carregar_dataset(dataset)

    # Fixar o modelo é o que torna a rodada comparável: sem `--modelo`, a cadeia
    # de fallback pode responder com um provider diferente a cada pergunta (cota
    # do tier gratuito estourando), e o resultado deixa de medir a config para
    # medir "qual modelo respondeu". Ver eval/analise-telemetria-2026-08-27.md §10.
    if modelo is None:
        typer.secho(
            "aviso: sem --modelo, a cadeia de fallback pode variar o provider entre "
            "as perguntas — a rodada não fica comparável. Ex.: -m huggingface:"
            f"{settings.hf_model}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if timeout is not None:
        # Lido na construção dos providers (`providers/chain`), que é preguiçosa
        # e só acontece na 1ª pergunta — então basta ajustar antes de `_rodar`.
        settings.llm_timeout = timeout

    telemetry.configurar_logs()
    telemetry_store.habilitar()
    telemetry.set_canal(_CANAL)

    if limpar_cache:
        removidos = clear_cache()
        typer.secho(f"cache limpo: {removidos} entrada(s) removida(s).",
                    fg=typer.colors.CYAN, err=True)

    linhas = _rodar(itens, modelo)

    if saida is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        saida = Path("eval/resultados") / f"{timestamp}.{formato}"
    _salvar(linhas, saida, formato)

    typer.secho(f"\nResultado salvo em {saida}", fg=typer.colors.CYAN)
    _resumo(linhas)


if __name__ == "__main__":
    app()
