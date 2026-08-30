"""Mede a distribuição de tamanho dos chunks do corpus e ajuda a calibrar CHUNK_SIZE.

    python -m scripts.chunk_stats                 # todos os assuntos de data/raw/
    python -m scripts.chunk_stats canvas          # só uma pasta
    python -m scripts.chunk_stats --sem-tokenizer # não carrega o tokenizer do E5

Existe por causa da RET-5: `CHUNK_SIZE` é "quase inerte" hoje porque o
`PyPDFLoader` entrega **um Document por página** e o splitter nunca junta
páginas — quem define a granularidade real é a página, não o `CHUNK_SIZE`.
Calibrar para CIMA não tem efeito (não há o que juntar); para BAIXO, tem
(quebra as páginas densas). Este script mostra os números concretos do corpus
ATUAL para decidir o valor com base na **mediana**, não no chute:

1. distribuição do tamanho das PÁGINAS (pré-split) — o teto natural;
2. distribuição do tamanho dos CHUNKS no `CHUNK_SIZE` vigente;
3. simulação: com cada `CHUNK_SIZE` candidato, quantos chunks saem, qual a
   mediana, e quantas páginas seriam de fato quebradas.

Sem banco e sem modelo de embeddings — só os loaders + o splitter. O tokenizer
do E5 é carregado só para converter caracteres em tokens (é em tokens que o
limite de 512 do modelo é medido); com `--sem-tokenizer` usa uma estimativa.
"""

import statistics
from pathlib import Path

import typer
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import DATA_RAW_DIR, settings
from app.ingestion.loaders.registry import loader_for, supported_extensions

app = typer.Typer(add_completion=False, help="Distribuição de tamanho dos chunks (RET-5).")

# Estimativa usada quando o tokenizer não está disponível. ~3.9 char/token é o
# que o próprio corpus mediu com o tokenizer do E5 (ver o comentário de
# `embedding_model` em app/core/config.py: mediana 92 tokens para ~370 chars).
_CHARS_POR_TOKEN = 3.9

_PERCENTIS = (10, 25, 50, 75, 90, 99)


def _percentis(valores: list[int]) -> dict[str, float]:
    """min / p10 / p25 / mediana / p75 / p90 / p99 / max / média de uma lista."""
    if not valores:
        return {}
    ordenados = sorted(valores)
    saida: dict[str, float] = {"min": ordenados[0], "max": ordenados[-1]}
    for p in _PERCENTIS:
        # interpolação simples (o `statistics.quantiles` exige n>=2 e muda de API
        # entre versões; aqui o índice direto basta para relatório)
        k = (len(ordenados) - 1) * p / 100
        baixo = ordenados[int(k)]
        alto = ordenados[min(int(k) + 1, len(ordenados) - 1)]
        saida[f"p{p}"] = round(baixo + (alto - baixo) * (k - int(k)), 1)
    saida["media"] = round(statistics.fmean(ordenados), 1)
    return saida


def _tokenizer(desligado: bool):
    if desligado:
        return None
    try:
        from transformers import AutoTokenizer

        try:
            return AutoTokenizer.from_pretrained(settings.embedding_model, local_files_only=True)
        except OSError:
            return AutoTokenizer.from_pretrained(settings.embedding_model)
    except Exception as exc:  # noqa: BLE001 - a estimativa cobre qualquer falha
        typer.secho(f"tokenizer indisponível ({exc}); usando estimativa de "
                    f"{_CHARS_POR_TOKEN} char/token", fg=typer.colors.YELLOW, err=True)
        return None


def _tokens(texto: str, tok) -> int:
    if tok is None:
        return round(len(texto) / _CHARS_POR_TOKEN)
    return len(tok.encode(texto, add_special_tokens=False))


def _paginas(assuntos: list[str]) -> list[str]:
    """Texto de cada Document (página, no caso de PDF) das pastas pedidas."""
    extensoes = supported_extensions()
    textos: list[str] = []
    for assunto in assuntos:
        pasta = DATA_RAW_DIR / assunto
        if not pasta.is_dir():
            typer.secho(f"pasta inexistente: {pasta}", fg=typer.colors.YELLOW, err=True)
            continue
        for path in sorted(pasta.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in extensoes:
                continue
            loader = loader_for(path)
            if loader is None:
                continue
            docs = loader.load()
            textos.extend(d.page_content for d in docs if d.page_content.strip())
            typer.echo(f"  {path.name}: {len(docs)} documento(s)/página(s)", err=True)
    return textos


def _split(textos: list[str], chunk_size: int) -> list[str]:
    """Aplica o mesmo splitter da ingestão, uma página por vez (nunca junta
    páginas — igual `chunker.split_documents`), com o `chunk_size` pedido."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return [
        pedaco
        for texto in textos
        for pedaco in splitter.split_text(texto)
        if pedaco.strip()
    ]


def _linha_dist(rotulo: str, valores: list[int], unidade: str) -> None:
    d = _percentis(valores)
    if not d:
        typer.echo(f"{rotulo}: (vazio)")
        return
    typer.echo(
        f"{rotulo:<22} n={len(valores):<5} "
        f"min={d['min']:.0f} p25={d['p25']:.0f} "
        f"mediana={d['p50']:.0f} p75={d['p75']:.0f} p90={d['p90']:.0f} "
        f"p99={d['p99']:.0f} max={d['max']:.0f}  ({unidade})"
    )


@app.command()
def main(
    assunto: str | None = typer.Argument(
        None, help="Pasta de data/raw/ (canvas, puc-digital...). Vazio = todas."
    ),
    sem_tokenizer: bool = typer.Option(
        False, "--sem-tokenizer", help="Não carrega o tokenizer do E5; estima tokens por char."
    ),
) -> None:
    if assunto:
        assuntos = [assunto]
    else:
        assuntos = sorted(p.name for p in DATA_RAW_DIR.iterdir() if p.is_dir()) \
            if DATA_RAW_DIR.is_dir() else []
    if not assuntos:
        raise typer.BadParameter(f"nada em {DATA_RAW_DIR}")

    typer.secho(f"Lendo {', '.join(assuntos)}...", fg=typer.colors.CYAN, err=True)
    textos = _paginas(assuntos)
    if not textos:
        raise typer.BadParameter("nenhuma página carregada (loaders? pasta vazia?)")

    tok = _tokenizer(sem_tokenizer)

    pag_chars = [len(t) for t in textos]
    pag_tokens = [_tokens(t, tok) for t in textos]

    atual = settings.chunk_size
    chunks_atuais = _split(textos, atual)
    ch_chars = [len(c) for c in chunks_atuais]
    ch_tokens = [_tokens(c, tok) for c in chunks_atuais]

    typer.secho("\n== PÁGINAS (pré-split) — é o teto natural da granularidade ==", bold=True)
    _linha_dist("página", pag_chars, "caracteres")
    _linha_dist("página", pag_tokens, "tokens E5")

    typer.secho(f"\n== CHUNKS no CHUNK_SIZE atual ({atual}) ==", bold=True)
    _linha_dist("chunk", ch_chars, "caracteres")
    _linha_dist("chunk", ch_tokens, "tokens E5")

    mediana_pag = _percentis(pag_chars)["p50"]
    p90_pag = _percentis(pag_chars)["p90"]
    candidatos = sorted({
        int(mediana_pag),
        int(mediana_pag * 1.5),
        int(p90_pag),
        500, 700, atual,
    })

    typer.secho("\n== SIMULAÇÃO — CHUNK_SIZE candidato ==", bold=True)
    typer.echo(f"{'CHUNK_SIZE':>10} {'nº chunks':>10} {'mediana(char)':>14} "
               f"{'p90(char)':>11} {'páginas quebradas':>18}")
    for cand in candidatos:
        pedacos = _split(textos, cand)
        chars = [len(c) for c in pedacos]
        quebradas = sum(1 for c in pag_chars if c > cand)
        marca = "  <- atual" if cand == atual else ""
        typer.echo(
            f"{cand:>10} {len(pedacos):>10} "
            f"{_percentis(chars)['p50']:>14.0f} {_percentis(chars)['p90']:>11.0f} "
            f"{quebradas:>10} / {len(pag_chars):<5}{marca}"
        )

    typer.secho("\n== Leitura ==", bold=True)
    if p90_pag <= atual:
        typer.echo(
            f"90% das páginas já cabem em {atual} chars (p90={p90_pag:.0f}). Subir "
            f"CHUNK_SIZE não muda NADA. Só faz sentido descer."
        )
    alvo = int(round(mediana_pag / 50) * 50)
    typer.echo(
        f"Para uniformizar os chunks perto da MEDIANA da página "
        f"(~{mediana_pag:.0f} chars), CHUNK_SIZE de ~{alvo} deixa a página típica "
        f"inteira e quebra só as densas. Confira na tabela acima o nº de chunks e "
        f"as páginas quebradas antes de fixar — e reingira depois de mudar "
        f"(`python -m scripts.ingest`)."
    )


if __name__ == "__main__":
    app()
