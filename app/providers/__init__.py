"""Camada de provedor de IA — o único lugar do projeto que conhece um SDK de LLM.

Trocar de provedor, acrescentar um ou mudar a ordem de prioridade não sai daqui.
Quem consome (`app/agent/responder.py`) enxerga só `chain.get_chat_model()`, e o
que ele devolve responde a `invoke(mensagens) -> AIMessage` — o mesmo contrato
de antes, quando havia um provedor só.

    base.py            interface LLMProvider + a regra de quando cair p/ o próximo
    gemini.py          provider 1 (gemini-3.6-flash)
    openai_compat.py   providers 2 e 3 (Groq e OpenRouter, API da OpenAI)
    chain.py           ProviderChain: a cadeia de fallback e a montagem via .env
    embeddings.py      modelo de embeddings (local, HuggingFace — não é chat)

De propósito SEM reexportar nada aqui: `embeddings.py` arrasta
`sentence-transformers` (e, com ele, o torch), que leva dezenas de segundos para
importar. Um `__init__` que importasse tudo faria qualquer `import app.providers.
chain` — inclusive o dos testes da cadeia, que não tocam em embedding nenhum —
pagar esse custo. Importe sempre do submódulo.
"""
