"""Recomenda um valor para RERANKER_THRESHOLD a partir de rodadas do eval_run.

    # 1. rode o dataset com o reranker LIGADO (grava score_top na escala do cross-encoder)
    RERANKER_ENABLED=true python -m scripts.eval_run eval/perguntas/perguntas.jsonc \
      -m gemini:gemini-3.6-flash -c -o eval/resultados/reranker-ON-1.json

    # 2. (recomendado) repita 2-3x para ter mediana por pergunta — RET-2
    #    a mesma pergunta oscila de score entre rodadas; a mediana é o sinal estável.

    # 3. varra os thresholds em cima do(s) arquivo(s), sem reprocessar nada:
    python -m scripts.calibrar_reranker eval/resultados/reranker-ON-*.json

Por que existe (RET-7): com `RERANKER_ENABLED=true` o `retrieve` tem dois cortes
e, com `RERANKER_THRESHOLD=0.0`, NENHUM deles filtra pergunta fora de domínio —
o 1º estágio (E5) não aplica `RELEVANCE_THRESHOLD` nesse ramo, e `score >= 0.0`
é sempre verdade. A defesa (b) do backlog é calibrar `RERANKER_THRESHOLD` acima
de 0. Este script faz a conta.

O QUE ELE MEDE — e o que NÃO mede:

Um `RERANKER_THRESHOLD=T` só muda o ROTEAMENTO de uma pergunta quando derruba
TODOS os chunks dela — ou seja, quando `score_top < T` (o topo é o último a
cair). Aí a pergunta perde o caminho `base` e vai para web/secretaria. Então a
varredura precisa só de `score_top` (escala do cross-encoder) por pergunta +
`origem_esperada` — que o `eval_run` já grava. NÃO precisa da
`resposta_referencia` da suíte T-1: isso é para o LLM-judge de fidelidade, uma
pergunta diferente ("a resposta bate com o guia?").

Dois grupos de perguntas importam:

- **cobre pela base** (`origem_esperada=base` e o agente acertou por `base`):
  precisam continuar com `score_top >= T`. Um T que derruba uma destas é
  REGRESSÃO — o alvo é `quebra = 0`.
- **alvo de corte** (`origem_esperada` = `nenhuma`/`encaminhado`, mas o agente
  respondeu por `base` — o vazamento do RET-7, ex. Q4 fotossíntese): queremos
  `score_top < T` para elas passarem a ser cortadas.

O melhor T é o MAIOR valor com `quebra = 0`. Se nesse ponto ele não corta
nenhum vazamento (as duas faixas de score se sobrepõem), não há threshold limpo:
o relatório diz isso e apel para a defesa (a) — piso de E5 no 1º estágio.

Sem banco, sem modelo, sem rede — lê só o JSON de resultado do `eval_run`.
"""

import json
import statistics
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Calibra RERANKER_THRESHOLD (RET-7, defesa b).")

# origem_esperada que significa "a base NÃO devia responder isto". `web` fica de
# fora: uma pergunta de fonte oficial pode legitimamente ser coberta pela base
# pré-crawlada (KB-3), então `base` nela não é vazamento inequívoco.
_ALVO_DE_CORTE = {"nenhuma", "encaminhado"}


def _carregar(caminhos: list[Path]) -> list[dict]:
    linhas: list[dict] = []
    for caminho in caminhos:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        for linha in dados:
            linha["_arquivo"] = caminho.name
            linhas.append(linha)
    return linhas


def _por_pergunta(linhas: list[dict]) -> dict[str, dict]:
    """Agrupa as N rodadas por pergunta e reduz `score_top` à MEDIANA.

    A mesma pergunta muda de score entre rodadas (o E5 e o cross-encoder são
    determinísticos, mas o conjunto de candidatos recuperados varia com o
    conteúdo indexado e o corte de topo). A mediana de 2-3 rodadas é o número
    que o RET-2 pede antes de virar `if`.
    """
    grupos: dict[str, list[dict]] = {}
    for linha in linhas:
        grupos.setdefault(linha["pergunta"], []).append(linha)

    saida: dict[str, dict] = {}
    for pergunta, rodadas in grupos.items():
        scores = [r["score_top"] for r in rodadas if r.get("score_top") is not None]
        brutos = [r["score_top_bruto"] for r in rodadas if r.get("score_top_bruto") is not None]
        ref = rodadas[-1]
        saida[pergunta] = {
            "origem_esperada": ref["origem_esperada"],
            # a origem obtida mais comum entre as rodadas
            "origem_obtida": statistics.mode(r.get("origem_obtida") for r in rodadas),
            "acertou_base": any(r.get("acertou") and r.get("origem_obtida") == "base"
                                for r in rodadas),
            "score_top": statistics.median(scores) if scores else None,
            "score_top_bruto": statistics.median(brutos) if brutos else None,
            "n_rodadas": len(rodadas),
            "reranker_aplicado": any(r.get("reranker_aplicado") for r in rodadas),
        }
    return saida


def _distribuicao(rotulo: str, valores: list[float]) -> None:
    if not valores:
        typer.echo(f"  {rotulo:<26} (nenhuma)")
        return
    v = sorted(valores)
    typer.echo(
        f"  {rotulo:<26} n={len(v):<3} min={v[0]:.3f} "
        f"mediana={statistics.median(v):.3f} max={v[-1]:.3f}"
    )


@app.command()
def main(
    resultados: list[Path] = typer.Argument(
        ..., help="Um ou mais JSON de eval_run rodados com RERANKER_ENABLED=true."
    ),
    passo: float = typer.Option(0.02, "--passo", help="Granularidade da varredura de T."),
    tolerancia: int = typer.Option(
        0, "--tolerancia",
        help="Nº de perguntas `base` que pode quebrar na recomendação (default 0).",
    ),
) -> None:
    linhas = _carregar(resultados)
    if not linhas:
        raise typer.BadParameter("nenhuma linha nos arquivos passados")

    perguntas = _por_pergunta(linhas)
    com_score = {p: d for p, d in perguntas.items() if d["score_top"] is not None}

    aplicado = sum(1 for d in com_score.values() if d["reranker_aplicado"])
    if aplicado == 0:
        typer.secho(
            "nenhuma linha tem reranker_aplicado=true — a(s) rodada(s) foram feitas com "
            "RERANKER_ENABLED=false? `score_top` aqui precisa ser o do cross-encoder.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if aplicado < len(com_score):
        typer.secho(
            f"aviso: {len(com_score) - aplicado} pergunta(s) sem reranker_aplicado — "
            "misturando escalas. Rode tudo com RERANKER_ENABLED=true.",
            fg=typer.colors.YELLOW, err=True,
        )

    cobre_base = {p: d for p, d in com_score.items()
                  if d["origem_esperada"] == "base" and d["acertou_base"]}
    alvo_corte = {p: d for p, d in com_score.items()
                  if d["origem_esperada"] in _ALVO_DE_CORTE and d["origem_obtida"] == "base"}
    base_ja_perdida = {p: d for p, d in com_score.items()
                       if d["origem_esperada"] == "base" and not d["acertou_base"]}

    typer.secho(
        f"\n{len(com_score)} pergunta(s) chegaram ao retrieval "
        f"({len(linhas)} linha(s), {max(d['n_rodadas'] for d in com_score.values())} rodada(s) máx.)",
        bold=True,
    )
    typer.echo(f"  cobre pela base (não pode quebrar): {len(cobre_base)}")
    typer.echo(f"  alvo de corte (vazamento RET-7):    {len(alvo_corte)}")
    typer.echo(f"  base já perdida hoje (T não ajuda): {len(base_ja_perdida)}")

    typer.secho("\n== score_top (escala do cross-encoder) por grupo ==", bold=True)
    _distribuicao("cobre pela base", [d["score_top"] for d in cobre_base.values()])
    _distribuicao("alvo de corte", [d["score_top"] for d in alvo_corte.values()])
    typer.secho("\n== score_top_bruto (E5) — referência p/ a defesa (a) ==", bold=True)
    _distribuicao("cobre pela base", [d["score_top_bruto"] for d in cobre_base.values()
                                      if d["score_top_bruto"] is not None])
    _distribuicao("alvo de corte", [d["score_top_bruto"] for d in alvo_corte.values()
                                    if d["score_top_bruto"] is not None])

    if not cobre_base:
        raise typer.BadParameter(
            "nenhuma pergunta `base` acertou — não dá para calibrar sem saber o "
            "piso do que a base cobre. Confira a rodada."
        )

    # Varredura. O T ótimo está sempre num ponto logo abaixo de algum score
    # observado; o passo fixo é suficiente para o relatório.
    teto = max(d["score_top"] for d in com_score.values())
    ts = [round(i * passo, 4) for i in range(int(teto / passo) + 2)]

    typer.secho("\n== varredura de RERANKER_THRESHOLD ==", bold=True)
    typer.echo("  (quebra base = acerto real perdido; corta vazamento = lixo RET-7 barrado)")
    typer.echo(f"  {'T':>6} {'quebra base':>12} {'corta vazamento':>16}")
    recomendado = None
    for t in ts:
        quebra = sum(1 for d in cobre_base.values() if d["score_top"] < t)
        corta = sum(1 for d in alvo_corte.values() if d["score_top"] < t)
        marca = ""
        if quebra <= tolerancia and corta > 0:
            recomendado = t
            marca = "  <-"
        if quebra or corta or marca:
            typer.echo(f"  {t:>6.2f} {quebra:>12} {corta:>16}{marca}")

    typer.secho("\n== recomendação ==", bold=True)
    if recomendado is None:
        pior_corte = max((d["score_top"] for d in alvo_corte.values()), default=None)
        melhor_base = min((d["score_top"] for d in cobre_base.values()), default=None)
        typer.secho(
            "Não há threshold limpo: o vazamento pontua ACIMA do menor acerto real "
            f"(vazamento até {pior_corte:.3f}, base a partir de {melhor_base:.3f}). "
            "As faixas se sobrepõem — subir RERANKER_THRESHOLD o bastante para cortar "
            "o lixo derruba acerto real.\n"
            "→ Vá para a defesa (a): piso de E5 no 1º estágio (filtrar os candidatos "
            "por RELEVANCE_THRESHOLD ANTES do cross-encoder). Ver RET-7 no backlog.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)

    fixados = [p for p, d in alvo_corte.items() if d["score_top"] < recomendado]
    quebrados = [p for p, d in cobre_base.items() if d["score_top"] < recomendado]
    typer.secho(
        f"RERANKER_THRESHOLD = {recomendado:.2f}", fg=typer.colors.GREEN, bold=True
    )
    typer.echo(f"  corta {len(fixados)} vazamento(s), quebra {len(quebrados)} base "
               f"(tolerância = {tolerancia})")
    for p in fixados:
        typer.echo(f"    corta: {p[:80]}")
    for p in quebrados:
        typer.secho(f"    QUEBRA: {p[:80]}", fg=typer.colors.RED)

    typer.secho(
        "\nAntes de fixar no .env: rode a confirmação com o valor recomendado no "
        "dataset inteiro (PT + fidelidade EN) e cheque que nada que era `base` virou "
        "`nenhuma`:\n"
        f"  RERANKER_THRESHOLD={recomendado:.2f} python -m scripts.eval_run "
        "eval/perguntas/perguntas.jsonc -m <modelo> -c",
        fg=typer.colors.CYAN,
    )


if __name__ == "__main__":
    app()
