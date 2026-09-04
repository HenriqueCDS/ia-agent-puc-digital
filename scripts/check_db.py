"""Testa a conexão com o Postgres do `.env`, sem carregar o modelo de embeddings.

    python -m scripts.check_db

Rápido de propósito: `scripts.list_ingested` também prova a conexão, mas passa
por `get_vector_store()`, que lê ~1GB de pesos do disco. Aqui é só o banco —
serve para validar um `DATABASE_URL` novo (trocar de Supabase, subir o Docker)
antes de gastar tempo com ingestão.

Confere, em ordem:
  1. a URL parseia e tem os campos esperados (driver, host, senha URL-encoded);
  2. o servidor responde e é a versão/database que se espera;
  3. a extensão `vector` (pgvector) está instalada;
  4. a coleção de `COLLECTION_NAME` existe e quantos chunks tem.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import typer

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings

app = typer.Typer(add_completion=False, help="Testa a conexão com o Postgres do .env.")

_OK = typer.style("OK  ", fg=typer.colors.GREEN)
_ERRO = typer.style("ERRO", fg=typer.colors.RED)
_AVISO = typer.style("!   ", fg=typer.colors.YELLOW)


def _driver_normalizado(url: str) -> str:
    """Garante o driver `psycopg` (v3) — o mesmo que o resto do projeto usa.

    `postgresql://...` sem sufixo faz o SQLAlchemy procurar o `psycopg2`, que
    não é dependência daqui: o erro seria `ModuleNotFoundError`, não um
    diagnóstico de conexão. Troca só o esquema, mantém o resto.
    """
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _diagnostica_url(url: str) -> list[str]:
    """Avisos sobre erros comuns de montar a URL do Supabase. Não bloqueia."""
    avisos: list[str] = []
    partes = urlsplit(url)
    netloc = partes.netloc

    # '@' literal na senha quebra o parse: o host passa a ser o trecho depois do
    # ÚLTIMO '@' e a porta é lida errado. Mais de um '@' no netloc é o sintoma.
    if netloc.count("@") > 1:
        avisos.append(
            "a senha tem '@' sem escapar — URL-encode como %40 "
            "(ex.: `H054169@c23` -> `H054169%40c23`), senão host e porta "
            "são lidos errado"
        )
    else:
        # ':' extra na senha (além do separador user:pass) manda parte da senha
        # para o campo de porta.
        userinfo = netloc.rsplit("@", 1)[0]
        _, _, senha = userinfo.partition(":")
        if ":" in senha:
            avisos.append("a senha tem ':' sem escapar — URL-encode como %3A")

    host = partes.hostname or ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        avisos.append(
            "host `db.<ref>.supabase.co` é a conexão DIRETA, IPv6-only — de "
            "muitas redes não roteia. Use o Session pooler: "
            "`postgres.<ref>@aws-0-<regiao>.pooler.supabase.com:5432`"
        )
    if "pooler.supabase.com" in host and not (partes.username or "").startswith("postgres."):
        avisos.append(
            "no pooler do Supabase o usuário é `postgres.<ref>`, não `postgres`"
        )
    return avisos


@app.command()
def main() -> None:
    url = _driver_normalizado(settings.database_url)
    partes = urlsplit(url)

    typer.echo(f"driver   : {partes.scheme}")
    typer.echo(f"host     : {partes.hostname}:{partes.port or 5432}")
    typer.echo(f"database : {(partes.path or '/').lstrip('/') or '(default)'}")
    typer.echo(f"usuário  : {partes.username}")

    for aviso in _diagnostica_url(settings.database_url):
        typer.echo(f"{_AVISO} {aviso}")

    engine = create_engine(url, connect_args={"connect_timeout": 8})
    try:
        with engine.connect() as conn:
            versao = conn.execute(text("SELECT version()")).scalar_one()
            db_atual = conn.execute(text("SELECT current_database()")).scalar_one()
            typer.echo(f"{_OK} conectado — {db_atual}")
            typer.echo(f"     {versao.split(' on ')[0]}")

            pgvector = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
            if pgvector:
                typer.echo(f"{_OK} extensão pgvector {pgvector}")
            else:
                typer.echo(
                    f"{_AVISO} extensão `vector` não instalada — "
                    "`CREATE EXTENSION vector;` (a ingestão tenta criar sozinha)"
                )

            existe_colecao = conn.execute(
                text("SELECT to_regclass('public.langchain_pg_collection')")
            ).scalar_one_or_none()
            if not existe_colecao:
                typer.echo(
                    f"{_AVISO} nenhuma coleção ainda — rode `python -m scripts.ingest`"
                )
            else:
                chunks = conn.execute(
                    text(
                        """
                        SELECT count(e.*)
                        FROM langchain_pg_embedding e
                        JOIN langchain_pg_collection c ON c.uuid = e.collection_id
                        WHERE c.name = :nome
                        """
                    ),
                    {"nome": settings.collection_name},
                ).scalar_one()
                marca = _OK if chunks else _AVISO
                typer.echo(
                    f"{marca} coleção '{settings.collection_name}': {chunks} chunk(s)"
                )
    except SQLAlchemyError as exc:
        causa = getattr(exc, "orig", exc)
        typer.echo(f"{_ERRO} {str(causa).strip().splitlines()[0]}")
        raise typer.Exit(code=1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    app()
