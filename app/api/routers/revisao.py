"""Revisão manual de fidelidade (apoio ao `scripts.eval_run`).

`scripts/eval_run.py` deixa claro na docstring o que a taxa de `acertou` NÃO
mede: ela compara só `origem_obtida` com `origem_esperada`. Uma resposta que
inventa um prazo, cita a página errada ou responde pela metade sai como acerto
desde que tenha ido pelo caminho certo. A conferência de verdade é à mão, lendo
a `resposta` inteira de cada linha do JSON da rodada.

Ler isso num editor, rolando um array de 15+ objetos, é lento e fácil de perder
o fio. Esta rota serve uma página que mostra UMA pergunta por vez com a resposta
formatada, as fontes e o `criterio` do dataset em destaque, e três botões
(satisfeito / insatisfeito / pular) que avançam para a próxima — e no fim monta
um relatório com a contagem e a lista dos insatisfeitos.

SOBRE SERVIR OS ARQUIVOS: diferente da demo (T3.1), esta página não chama
`/v1/ask` — não há chave de API para injetar nem orçamento de LLM em jogo. O
que ela expõe são os JSONs de `eval/resultados/`, que já estão versionados no
repositório e passaram por `pii.mascarar` na escrita (ver `_linha` no
eval_run). O `nome` do arquivo na URL é revalidado contra o diretório real
antes de qualquer leitura (`_arquivo_de_resultado`): o parâmetro é
direcionamento, a garantia é a checagem local — mesmo princípio da allowlist
da busca web.

A rota inteira sai do ar com `REVISAO_ENABLED=false`, para produção, onde
`eval/resultados/` normalmente nem existe.
"""

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PAGINA = _PROJECT_ROOT / "app" / "static" / "revisao.html"
_RESULTADOS_DIR = _PROJECT_ROOT / "eval" / "resultados"

# Timestamp do eval_run (`%Y%m%dT%H%M%SZ`) ou um `--saida` manual: letras,
# dígitos, `-`, `_`, `.`, terminando em `.json`. Sem `/`, sem `..` — o que
# sobra não alcança nada fora de `_RESULTADOS_DIR`, e a revalidação de path
# abaixo é a checagem que de fato garante isso.
_NOME_VALIDO = re.compile(r"^[A-Za-z0-9._-]+\.json$")


def _arquivo_de_resultado(nome: str) -> Path:
    """Resolve `nome` para um arquivo DENTRO de `_RESULTADOS_DIR`, ou 404.

    A regex barra o óbvio; esta função é a garantia: resolve o caminho e
    confirma que o pai é exatamente o diretório de resultados. `strict=False`
    no `resolve` para o 404 (e não um 500) cobrir o arquivo inexistente.
    """
    if not _NOME_VALIDO.match(nome):
        raise HTTPException(status_code=404, detail="arquivo não encontrado")

    alvo = (_RESULTADOS_DIR / nome).resolve()
    if alvo.parent != _RESULTADOS_DIR.resolve() or not alvo.is_file():
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    return alvo


@router.get("/revisao", response_class=HTMLResponse, include_in_schema=False)
def revisao() -> HTMLResponse:
    return HTMLResponse(_PAGINA.read_text(encoding="utf-8"))


@router.get("/revisao/resultados", include_in_schema=False)
def listar_resultados() -> JSONResponse:
    """Nomes dos JSONs de `eval/resultados/`, mais recente primeiro.

    Sem o diretório (produção, checkout raso), devolve lista vazia — a página
    então só aceita upload de um arquivo local.
    """
    if not _RESULTADOS_DIR.is_dir():
        return JSONResponse({"arquivos": []})

    arquivos = sorted(
        (p for p in _RESULTADOS_DIR.glob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return JSONResponse(
        {
            "arquivos": [
                {"nome": p.name, "modificado": int(p.stat().st_mtime)} for p in arquivos
            ]
        }
    )


@router.get("/revisao/resultados/{nome}", include_in_schema=False)
def obter_resultado(nome: str) -> JSONResponse:
    """O array de linhas de uma rodada. Erro de parse vira 422, não 500:
    um arquivo `--formato csv` ou truncado é problema do que se pediu abrir."""
    arquivo = _arquivo_de_resultado(nome)
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"{nome} não é um JSON de resultado válido: {exc}"
        ) from None
    if not isinstance(dados, list):
        raise HTTPException(status_code=422, detail=f"{nome} não é uma lista de linhas")
    return JSONResponse(dados)
