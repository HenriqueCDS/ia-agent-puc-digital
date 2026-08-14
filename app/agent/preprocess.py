"""Normalização da entrada do usuário para uma `Query` de texto.

Hoje é quase um passa-adiante. Existe como módulo próprio porque é o ponto onde
as entradas não-textuais entram no futuro:

PONTO DE EXTENSÃO — interpretação de print de tela:
    se query.attachments:
        descricao = descrever_imagem(anexo)   # Gemini multimodal
        query.text = f"{query.text}\n\n[Print enviado pelo usuário]: {descricao}"

O retriever e o responder continuam recebendo só texto — nada abaixo daqui muda.
"""

from app.core.models import Query


def normalize(query: Query) -> Query:
    query.text = query.text.strip()
    if not query.text:
        raise ValueError("Pergunta vazia.")

    if query.attachments:
        raise NotImplementedError(
            "Interpretação de imagens ainda não faz parte do escopo (v1)."
        )

    return query
