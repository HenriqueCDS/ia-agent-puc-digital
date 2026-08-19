"""API HTTP — entrypoint opcional (o fluxo principal da v1 é via CLI em scripts/).

Existe para que plugar um front ou um canal de atendimento depois seja só somar
rotas: toda a lógica está em app/agent/responder.py.

Rodar: uvicorn app.main:app --reload
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.responder import answer
from app.core.models import Query

app = FastAPI(title="Agente de Suporte Acadêmico", version="1.0.0")


class AskRequest(BaseModel):
    pergunta: str
    assunto: str | None = None


class AskResponse(BaseModel):
    resposta: str
    fontes: list[str]
    encontrou_na_base: bool
    # "base" | "web" | "nenhuma" — quem consome precisa saber que a resposta veio
    # de uma página pública, e não do material interno revisado.
    origem: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    try:
        resultado = answer(Query(text=req.pergunta, assunto=req.assunto))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return AskResponse(
        resposta=resultado.text,
        fontes=[c.citation for c in resultado.sources],
        encontrou_na_base=resultado.grounded,
        origem=resultado.origem,
    )
