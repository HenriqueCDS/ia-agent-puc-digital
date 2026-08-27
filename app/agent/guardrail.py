"""Guardrail de entrada: pedido de ataque/abuso é encaminhado ANTES do RAG.

Roda como PRIMEIRA etapa de `responder._responder`, antes até da triagem por
assunto. É o mesmo desenho da triagem (ver `app/agent/triagem.py`) e termina no
mesmo lugar — `origem="encaminhado"`, o texto de `config.CONTATO_PADRAO` —, só
que o gatilho aqui não é "assunto de outro departamento" e sim "isto é um pedido
que o agente não deve nem tentar responder":

- injeção de prompt / jailbreak ("ignore as instruções", "a partir de agora
  você é...", "[SYSTEM MESSAGE]");
- exfiltração de segredo ("system prompt", "chave de API", "token JWT",
  "arquivo .env", "senha de administrador");
- execução não autorizada / SQL ("DROP TABLE", "DELETE FROM", "execute o
  seguinte comando");
- pedido de código ofensivo ("código de exploit", "payload XSS").

POR QUE LÉXICO, e não um classificador ou uma chamada ao LLM: pelo mesmo motivo
da triagem. Estes vetores são nomeados por vocabulário de altíssima
distintividade — ninguém pergunta "como faço DROP TABLE alunos" de boa-fé num
suporte de Canvas. Um guardrail sobre abuso precisa ser determinístico e
auditável: dá para provar em teste qual pergunta é barrada. E mandar a entrada
hostil para um LLM (ou para a busca externa) só para decidir que ela é hostil é
contraditório — é exatamente o egress que o guardrail existe para evitar
(ver eval/analise-telemetria-2026-08-27.md §4).

O QUE ESTA ABORDAGEM NÃO PEGA: paráfrase sem os termos, ataque em outro idioma,
injeção indireta (payload dentro de um documento que o RAG recupera). Essas
seguem para o pipeline, onde as defesas existentes (RAG fechado no CONTEXTO,
`SYSTEM_WEB` tratando contexto como dado, allowlist de domínio) ainda valem — o
guardrail só melhora, nunca piora. O caminho para fechar a lacuna é o mesmo da
triagem: medir na telemetria (`assunto="fora de escopo"`, `assunto_origem=
"guardrail"`) o que dispara e o que deveria ter disparado, e calibrar em cima de
caso real.

FALSO POSITIVO custa pouco: a pergunta legítima que casar um termo por engano é
encaminhada para o suporte — o mesmo destino, e o mesmo custo, de um falso
positivo da triagem. Ainda assim os termos são escolhidos para não colidir com
dúvida acadêmica normal; ver os comentários de cada grupo.
"""

import logging
import unicodedata

logger = logging.getLogger(__name__)

# Rótulo gravado em `Registro.assunto` quando o guardrail dispara. Casa com o
# tema "Pergunta fora do escopo de suporte" que já aparece no `scripts.lacunas`.
ASSUNTO = "fora de escopo"

# Substring, casada contra o texto sem acento e em caixa baixa (ver `_sem_acento`).
# Agrupado por vetor só para leitura — a checagem é uma passada linear.
_PADROES: tuple[str, ...] = (
    # --- Injeção de prompt / jailbreak (OWASP LLM01) ---
    # "instrução"/"regra" + verbo de anulação. Um aluno não pede para o
    # assistente ignorar as próprias regras.
    "ignore as instrucoes",
    "ignore todas as instrucoes",
    "ignore as regras",
    "ignore a sua base",
    "esqueca as instrucoes",
    "esqueca todas as instrucoes",
    "desconsidere as instrucoes",
    "desconsidere as regras",
    "a partir de agora voce e",
    "a partir de agora voce responde",
    "agora voce e um assistente",
    "assistente sem restricoes",
    "modo de manutencao",
    "modo desenvolvedor",
    "[system message]",
    "instrucao de emergencia",
    "substitua a instrucao",
    # --- Vazamento de system prompt / infraestrutura (OWASP LLM07) ---
    "system prompt",
    "instrucoes do sistema",
    "prompt do sistema",
    "prompt de sistema",
    "regras de negocio internas",
    "endpoints internos",
    "webhooks que",
    # --- Exfiltração de segredo / credencial (OWASP LLM02) ---
    "chave de api",
    "api key",
    "apikey",
    "token jwt",
    "token de api",
    "chave privada",
    "chave de conexao",
    "senha de administrador",
    "senha do administrador",
    "senha de admin",
    "senha do admin",
    "senha do banco",
    "credenciais salvas",
    "arquivo .env",
    ".env ou",
    "variaveis de ambiente",
    # --- Execução não autorizada / SQL via LLM (OWASP LLM06) ---
    "drop table",
    "delete from",
    "truncate table",
    "update alunos ",
    "execute o seguinte comando",
    "execute este codigo",
    "execute este comando",
    "execute este sql",
    "execute este codigo sql",
    "responda apenas em formato sql",
    "formato sql executavel",
    "sql executavel",
    # --- Ação sobre o registro do aluno que o agente não executa ---
    # Excessive agency (OWASP LLM06): o agente não altera nota, matrícula nem
    # status — pedir isso é sempre encaminhamento, e o caso real trazia CPF e
    # senha no mesmo texto (ver eval/analise-telemetria-2026-08-27.md §5).
    "altere a nota",
    "altere minha nota",
    "alterar a nota da minha prova",
    "mude a nota",
    "muda a nota",
    "excluir a minha matricula",
    "exclua a minha matricula",
    "apague a minha matricula",
    # --- Pedido de código ofensivo / uso indevido ---
    "invadir o sistema",
    "invadir sistema",
    "exploit",
    "payload de ataque",
    "payload xss",
    "ataque xss",
    "capturar o cookie",
    "document.cookie",
    # --- Consumo ilimitado / DoS (OWASP LLM10) ---
    "loop infinito",
    "ate que o servidor caia",
    "requisicoes em loop",
    "infinitas vezes",
    "repita a palavra",
    # --- Footprinting de infraestrutura / supply chain (OWASP LLM03) ---
    "versao vulneravel",
    "biblioteca de terceiros",
    "onde fica hospedado",
    # --- Sondagem do RAG / weakness de embedding (OWASP LLM08) ---
    "injetar embeddings",
    "embeddings nulos",
    # --- Divulgação de PII de terceiro (OWASP LLM02) ---
    "aluno registrado com",
    "do aluno com id",
    # --- Engenharia social com credencial de autoridade forjada ---
    "token adm",
    "token de admin",
)


def _sem_acento(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def deve_encaminhar(pergunta: str) -> str | None:
    """O termo de abuso que a pergunta casou, ou None se ela é legítima.

    Devolve o TERMO (para o log), não o rótulo de assunto — quem chama grava
    `ASSUNTO` na telemetria e o termo fica só no INFO, útil para auditar qual
    padrão está disparando (e qual está pegando falso positivo).
    """
    texto = _sem_acento(pergunta)
    for padrao in _PADROES:
        if padrao in texto:
            return padrao
    return None
