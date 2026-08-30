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

O QUE `acertou` NÃO MEDE — leia antes de tirar conclusão de uma taxa daqui:
ele compara só `resultado.origem` com `origem_esperada`. Uma resposta que
inventa um prazo ou cita a página errada sai como acerto desde que tenha ido
pelo caminho certo. Por isso a linha do resultado traz a `resposta` inteira,
os scores do retrieval e o `criterio` de conferência manual do dataset: o
arquivo é a base da revisão à mão, não o veredito. Ver
eval/plano-testes-2026-08-28.md §1.

Flags que existem por causa de rate limit / cota do tier gratuito:

- `--modelo/-m` fixa UM modelo sem cadeia de fallback. Sem isso, a cadeia pode
  responder com um provider diferente a cada pergunta (o do topo estoura a
  cota no meio da rodada) e o resultado deixa de comparar a mesma coisa. Sem
  `-m`, o script avisa.
- `--limpar-cache/-c` apaga a `resposta_cache` antes de rodar — sem isso a
  rodada mede o cache, não o pipeline, e a única pergunta não-cacheada pode
  derrubar tudo num 413.
- `--timeout` define `LLM_TIMEOUT` desta execução, e o default é 20s (não os 30
  do `.env`): com o provider do topo sem cota, cada pergunta queima o timeout
  inteiro antes do fallback. Passe `--timeout 30` para medir com o valor de
  produção.

O modelo de embeddings é pré-carregado antes da 1ª pergunta (INF-8) — sem isso
o cold start de ~40s cairia no item 1 e inflaria o `ms_retrieve` dele.

FORMATO DO DATASET — cada item tem `pergunta` e `origem_esperada`
(`base`/`web`/`encaminhado`/`nenhuma`), e opcionalmente:

- `origem_tambem_ok`: lista de origens que também contam como acerto. `nenhuma` e
  `encaminhado` dão a MESMA mensagem ao aluno ("procure a secretaria"), então numa
  pergunta que o agente legitimamente não sabe as duas são acerto — mas só onde a
  distinção não importa: em PII/injeção, `nenhuma` significa que o payload chegou
  ao LLM antes da desistência, e continua sendo divergência (não põe `nenhuma` no
  `origem_tambem_ok` desses).
- `assunto`: filtra o retrieval pela pasta de `data/raw/` (`canvas`/`puc-digital`).
- `criterio`: texto livre com o que conferir à mão além da `origem`.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from app.agent.responder import answer
from app.core import pii, telemetry
from app.core.config import settings
from app.core.models import Answer, Query
from app.db import telemetry_store
from app.db.response_cache import clear_cache
from app.db.vector_store import aquecer
from app.providers.base import TodosProvidersFalharam

app = typer.Typer(add_completion=False, help="Roda o dataset de avaliação contra o agente.")

_CANAL = "eval"

# Desfecho de uma pergunta cuja rodada seguiu viva mas em que NENHUM provedor de
# LLM respondeu (cota estourada em todos, `Cancelled: 499` do Gemini sem chave
# de reserva de pé...). Fica como `origem_obtida` no lugar de `None` para que o
# resumo distinga "a infra caiu nesta pergunta" de "o agente roteou errado" —
# ver INF-6 em eval/backlog-problemas.md. Não é um valor de `models.Origem`: só
# o harness de avaliação o produz, e só a partir de `TodosProvidersFalharam`.
_ORIGEM_PROVEDORES_INDISPONIVEIS = "provedores_indisponivel"


def _origem_de_erro(erro: str | None) -> str | None:
    """`provedores_indisponivel` quando a falha foi a cadeia de LLM inteira.

    Casada pelo NOME da exceção (`_rodar` já serializou para `"Tipo: mensagem"`):
    amarra ao contrato de `TodosProvidersFalharam` sem reintroduzir o try/except
    tipado que `_rodar` deixou genérico de propósito. Qualquer outra falha
    (413 de formato, bug nosso) continua com `origem_obtida=None`.
    """
    if erro and erro.startswith(TodosProvidersFalharam.__name__ + ":"):
        return _ORIGEM_PROVEDORES_INDISPONIVEIS
    return None

# Campos do registro de telemetria copiados para cada linha do resultado. O
# arquivo passa a ser autossuficiente: dá para analisar a rodada sem cruzar com
# o `.jsonl` exportado, que era o que a §7 de eval/analise-telemetria-2026-08-27
# apontou como fonte de confusão.
_CAMPOS_DA_TELEMETRIA = (
    "chunks_recuperados",  # renomeado de n_chunks — ver _CAMPOS_SAIDA abaixo
    "score_top",
    "score_min",
    "score_mean",
    "alta_confianca",
    "provider",
    "chat_model",
    "cache_hit",
    "pii",
    "base_insuficiente",
    "web_insuficiente",
    "veto_escapou",
    "assunto_origem",
    "ms_total",
    "input_tokens",
    "output_tokens",
)

# `fontes_resposta`/`score_fonte_top` são as FONTES DA RESPOSTA (vazias quando a
# origem é `nenhuma`/`encaminhado`); `chunks_recuperados`/`score_top` são o que o
# RETRIEVAL trouxe (quase sempre 5). Os nomes antigos (`n_chunks`, `score_top`
# para as duas coisas) faziam o arquivo parecer contradizer a telemetria.
_CAMPOS_SAIDA = [
    "pergunta",
    "assunto",
    "origem_esperada",
    "origem_tambem_ok",
    "origem_obtida",
    "acertou",
    "grounded",
    "cached",
    "resposta",
    "fontes_resposta",
    "score_fonte_top",
    "fontes_citadas",
    *_CAMPOS_DA_TELEMETRIA,
    "erro",
    "criterio",
]


def _carregar_dataset(caminho: Path) -> list[dict]:
    itens = json.loads(caminho.read_text(encoding="utf-8"))
    for i, item in enumerate(itens):
        faltando = {"pergunta", "origem_esperada"} - item.keys()
        if faltando:
            raise typer.BadParameter(f"item {i} do dataset sem campo(s) {faltando}: {item}")
    return itens


def _origens_aceitas(item: dict) -> list[str]:
    """`origem_esperada` + os aliases de `origem_tambem_ok`.

    `nenhuma` e `encaminhado` produzem a MESMA mensagem para o aluno ("procure a
    secretaria") — a diferença é só interna (o `lacunas` conta `nenhuma` como
    "documento talvez faltando", `encaminhado` como "outro departamento"). Para
    uma pergunta que o agente legitimamente não sabe responder, as duas são
    acerto; `origem_tambem_ok: ["nenhuma"]` no dataset diz isso sem afrouxar os
    casos em que a distinção IMPORTA (PII/injeção: aí `nenhuma` significa que o
    payload chegou ao LLM antes da desistência, e continua sendo divergência).
    """
    return [item["origem_esperada"], *item.get("origem_tambem_ok", ())]


def _capturar_telemetria() -> list[dict]:
    """Encadeia um sink que guarda cada registro numa lista, e devolve a lista.

    ENCADEIA, não substitui: o destino já instalado (o Postgres, via
    `telemetry_store.habilitar`) continua recebendo. Chamado depois dele por
    isso — ver `persistencia_atual` em app/core/telemetry.py.

    A ordem é deliberada: guardar primeiro, delegar depois. Uma falha do banco
    não pode custar o registro que vai para o arquivo local, que é o resultado
    que o operador tem na mão quando a rodada termina.
    """
    registros: list[dict] = []
    anterior = telemetry.persistencia_atual()

    def sink(dados: dict) -> None:
        registros.append(dados)
        if anterior is not None:
            anterior(dados)

    telemetry.configurar_persistencia(sink)
    return registros


def _linha(item: dict, resultado: Answer | None, registro: dict, erro: str | None) -> dict:
    """Uma linha do resultado: o que o agente devolveu + o registro da telemetria.

    `resposta` passa por `pii.mascarar` pelo mesmo motivo que a telemetria já
    mascara a dela: o arquivo fica no repositório, e um dataset de teste pode
    trazer CPF/RA de propósito (ver eval/perguntas_teste3.json, bloco C) — a
    resposta pode ecoar o identificador que veio na pergunta. O mascaramento não
    atrapalha a conferência de fidelidade: ele só troca identificador pessoal,
    nunca o procedimento que se quer auditar.
    """
    aceitas = _origens_aceitas(item)
    origem_obtida = resultado.origem if resultado else _origem_de_erro(erro)
    return {
        "pergunta": item["pergunta"],
        "assunto": item.get("assunto"),
        "origem_esperada": item["origem_esperada"],
        "origem_tambem_ok": item.get("origem_tambem_ok") or None,
        "origem_obtida": origem_obtida,
        "acertou": bool(resultado and resultado.origem in aceitas),
        "grounded": resultado.grounded if resultado else None,
        "cached": resultado.cached if resultado else None,
        "resposta": pii.mascarar(resultado.text) if resultado else None,
        "fontes_resposta": len(resultado.sources) if resultado else None,
        "score_fonte_top": (
            round(resultado.sources[0].score, 4) if resultado and resultado.sources else None
        ),
        "fontes_citadas": (
            [c.citation for c in resultado.sources] if resultado else None
        ),
        **{
            campo: registro.get("n_chunks" if campo == "chunks_recuperados" else campo)
            for campo in _CAMPOS_DA_TELEMETRIA
        },
        # `erro` da telemetria já viria no dict acima se o campo estivesse na
        # lista; fica de fora de propósito para este aqui vencer — ele é o único
        # que também cobre a falha ANTES de `answer()` abrir o registro.
        "erro": erro or registro.get("erro"),
        # Passa adiante o que o dataset diz para conferir à mão (blocos B e C de
        # perguntas_teste3 não são avaliáveis por comparação de `origem`).
        "criterio": item.get("criterio"),
    }


def _rodar(itens: list[dict], modelo: str | None, registros: list[dict]) -> list[dict]:
    """Roda o dataset inteiro. Uma pergunta que falha NÃO derruba a rodada.

    O `try` existe por causa de um caso real: em 2026-08-27 uma única pergunta
    montou um prompt de ~11.6k tokens, o provider devolveu HTTP 413 e a rodada
    morreu no item 14 de 25 (ver a §10 daquela análise). Perder 1 item é um dado;
    perder a rodada é perder a tarde. O erro vai para a linha e para o stderr.
    """
    linhas = []
    for i, item in enumerate(itens, start=1):
        pergunta, assunto = item["pergunta"], item.get("assunto")
        typer.echo(f"[{i}/{len(itens)}] {pergunta[:70]}", err=True)

        marca = len(registros)
        resultado, erro = None, None
        try:
            resultado = answer(Query(text=pergunta, assunto=assunto, modelo=modelo))
        except Exception as exc:  # noqa: BLE001 - seguir a rodada é o comportamento desejado
            erro = f"{type(exc).__name__}: {exc}"
            typer.secho(f"    falhou: {erro[:160]}", fg=typer.colors.RED, err=True)

        # `answer()` emite exatamente um registro por chamada, e o emite também
        # quando levanta (o `finally` de `telemetry.registrar`) — então o que
        # apareceu depois da marca é o registro DESTA pergunta.
        registro = registros[marca] if len(registros) > marca else {}
        linhas.append(_linha(item, resultado, registro, erro))
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
            typer.echo(f"  esperado={l['origem_esperada']:<11} obtido={str(l['origem_obtida']):<11} {l['pergunta']}")

    indisponiveis = [l for l in linhas
                     if l["origem_obtida"] == _ORIGEM_PROVEDORES_INDISPONIVEIS]
    if indisponiveis:
        typer.secho(
            f"\n{len(indisponiveis)} pergunta(s) sem resposta: NENHUM provedor de LLM "
            "respondeu (a rodada seguiu). Re-rode estas isoladas com outra chave:",
            fg=typer.colors.RED,
        )
        for l in indisponiveis:
            typer.echo(f"  {l['pergunta'][:70]}")

    falhas = [l for l in linhas if l["erro"]]
    if falhas:
        typer.secho(f"\n{len(falhas)} pergunta(s) com erro (a rodada seguiu):",
                    fg=typer.colors.RED)
        for l in falhas:
            typer.echo(f"  {l['erro'][:120]}  <- {l['pergunta'][:50]}")

    # O aviso mais importante do resumo, e o motivo de `resposta` ter passado a
    # ser gravada: `acertou` compara SÓ a origem. Uma resposta com procedimento
    # inventado, citando a página errada, entra aqui como acerto. Ver
    # eval/plano-testes-2026-08-28.md §1.
    com_criterio = sum(1 for l in linhas if l["criterio"])
    if com_criterio:
        typer.secho(
            f"\n{com_criterio} item(ns) têm `criterio` de conferência MANUAL no dataset — "
            "a taxa acima mede só o roteamento, não a qualidade da resposta.",
            fg=typer.colors.YELLOW,
        )


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
    timeout: float = typer.Option(
        20.0, "--timeout",
        help="LLM_TIMEOUT (s) desta rodada. Default 20 (e não os 30 do .env): quando "
             "o provider do topo está sem cota, cada pergunta queima o timeout inteiro "
             "antes do fallback — INF-5. Passe 30 para usar o valor de produção.",
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

    # Lido na construção dos providers (`providers/chain`), que é preguiçosa e só
    # acontece na 1ª pergunta — então basta ajustar antes de `_rodar`.
    settings.llm_timeout = timeout
    typer.secho(f"LLM_TIMEOUT desta rodada: {timeout:g}s", fg=typer.colors.CYAN, err=True)

    telemetry.configurar_logs()
    telemetry_store.habilitar()
    # DEPOIS do `habilitar`: encadeia a captura sobre o sink do Postgres, sem
    # substituí-lo. Ver `_capturar_telemetria`.
    registros = _capturar_telemetria()
    telemetry.set_canal(_CANAL)

    if limpar_cache:
        removidos = clear_cache()
        typer.secho(f"cache limpo: {removidos} entrada(s) removida(s).",
                    fg=typer.colors.CYAN, err=True)

    # INF-8: carrega o modelo de embeddings ANTES da 1ª pergunta. Sem isto o
    # cold start (~40-65s) cairia no item 1, inflando o `ms_retrieve` dele e o
    # tempo de parede da rodada. Mesmo ponto de warm-up da API.
    typer.secho("warm-up: carregando modelo de embeddings...", fg=typer.colors.CYAN, err=True)
    aquecer()

    linhas = _rodar(itens, modelo, registros)

    if saida is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        saida = Path("eval/resultados") / f"{timestamp}.{formato}"
    _salvar(linhas, saida, formato)

    typer.secho(f"\nResultado salvo em {saida}", fg=typer.colors.CYAN)
    _resumo(linhas)


if __name__ == "__main__":
    app()
