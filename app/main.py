"""Entrypoint HTTP. Estrutura em app/api/ — ver app/api/app.py.

Rodar: uvicorn app.main:app --reload
"""

from app.api.app import create_app

app = create_app()
