"""CLI de exportação — extrai as linhas de um canal da tabela `telemetria`
para um arquivo `.jsonl`, uma linha por registro, com `criado_em`.

    python -m scripts.eval_export
    python -m scripts.eval_export --dias 7
    python -m scripts.eval_export --canal cli --saida producao.jsonl

Existe para inspecionar/auditar uma rodada fora do terminal (abrir no editor,
`jq`, planilha) sem escrever SQL cru toda vez. Por padrão exporta `canal='eval'`
— o canal gravado por `scripts.eval_run` — porque foi criado para revisar
rodadas de calibração (ver eval/analise-telemetria-2026-08-26.md), mas
`--canal` exporta qualquer outro (`cli`, `api`...).

Cada linha do `.jsonl` é o MESMO formato que já sai em stderr/no banco (ver
`app/core/telemetry.py`), mais o campo `criado_em`. `resposta` só vem
preenchida para `canal='eval'` — ver o campo em `telemetry.Registro`.
"""

import json
from pathlib import Path

import typer

from app.core.config import settings
from app.db.telemetry_store import exportar_canal

app = typer.Typer(add_completion=False, help="Exporta a telemetria de um canal para .jsonl.")


@app.command()
def main(
    canal: str = typer.Option("eval", help="Canal a exportar (o que scripts.eval_run grava)."),
    dias: int = typer.Option(7, help="Janela de busca, em dias."),
    saida: Path | None = typer.Option(
        None, "--saida", "-o", help="Onde salvar (default: eval/telemetria-<canal>-<data>.jsonl)."
    ),
) -> None:
    if dias > settings.telemetry_retention_days:
        typer.secho(
            f"aviso: a retenção da telemetria é de {settings.telemetry_retention_days} "
            f"dias; os dias além disso vêm vazios.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    linhas = exportar_canal(canal, dias=dias)

    if not linhas:
        typer.secho(
            f"Nenhum registro do canal '{canal}' nos últimos {dias} dia(s).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit()

    if saida is None:
        data = linhas[-1][0].strftime("%Y-%m-%d")
        saida = Path("eval") / f"telemetria-{canal}-{data}.jsonl"
    saida.parent.mkdir(parents=True, exist_ok=True)

    with saida.open("w", encoding="utf-8") as f:
        for criado_em, dados in linhas:
            registro = {"criado_em": criado_em.isoformat(), **dados}
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    typer.secho(f"{len(linhas)} registro(s) exportado(s) para {saida}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
