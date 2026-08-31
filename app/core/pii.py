"""Detecção e mascaramento de identificadores pessoais (T3.4 / LGPD).

Pergunta de aluno traz RA, CPF, e-mail e telefone com frequência ("meu RA é
12345678 e não consigo acessar"). O texto da pergunta nunca é persistido (ver
`app/core/telemetry.py`), mas dois campos derivados dela SÃO:

- `topico`, escrito pelo próprio LLM a partir da pergunta — nada impede o modelo
  de repetir o RA ali ("acesso ao Canvas do RA 12345678");
- `assunto`, que vem de metadata ou da triagem e por isso é seguro.

Então este módulo tem dois usos distintos, e é importante não confundi-los:

- `detectar()` produz um **alerta** — quais categorias apareceram na pergunta.
  É contável (quantas perguntas trazem CPF por semana?) e não guarda o valor.
- `mascarar()` é a **contenção** — passa em todo texto derivado antes de ele
  virar linha no banco.

Sem dependência nova: `re` e mais nada. Não é um detector completo de PII e não
tenta ser — é um filtro de alto sinal para os identificadores que de fato
aparecem no suporte acadêmico: CPF, RA/matrícula, e-mail, celular e a SENHA que
o aluno cola no texto ("minha senha é Aluno@2026, não entra"). A senha não é
LGPD no sentido estrito, mas é credencial — e o pior lugar para ela parar é o
prompt que segue para o provedor de LLM (ver `responder._sem_pii`, PII-1/PII-2).

FALSO NEGATIVO É ACEITÁVEL, FALSO POSITIVO NÃO. Um alerta que dispara em número
de protocolo ou em ano ("2024 2025") vira ruído e é ignorado em duas semanas —
por isso o CPF sem pontuação é validado pelos dígitos verificadores e o telefone
exige o 9 do celular. Ver os comentários de cada padrão.
"""

import re

# Ordem de aplicação — cada categoria é mascarada antes da seguinte, e isso não
# é estilo: senha primeiro porque "minha senha é joao@x.com" deve virar
# "senha é [senha]", não "[email]"; e-mail antes de CPF porque
# `ra12345678900@puc-campinas.edu.br` casaria CPF dentro do próprio endereço;
# CPF antes de telefone porque 11 dígitos seguidos casam os dois padrões.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*\w\b")

# 000.000.000-00 ou 00000000000. Os separadores são opcionais porque o aluno
# digita das duas formas.
_CPF = re.compile(r"(?<!\d)(\d{3})[.\s]?(\d{3})[.\s]?(\d{3})[-\s]?(\d{2})(?!\d)")

# RA/matrícula só é reconhecido COM a palavra que o nomeia por perto. Um número
# de 8 dígitos solto é indistinguível de protocolo, código de disciplina ou data
# — exigir o rótulo troca recall por precisão de propósito (ver o cabeçalho).
#
# O vão entre rótulo e número é `[^\d\n]{0,15}` e não `\W*`: o aluno escreve
# "meu RA é 12345678", "matrícula: 987654321", "RA do aluno 20231234" — tem
# palavra no meio, e `\W` (que não casa letra) perderia todos esses. O limite de
# 15 caracteres e o corte na quebra de linha são o que impede o rótulo de uma
# frase capturar um número da frase seguinte.
_RA = re.compile(
    r"\b(?:r\.?\s?a\.?|registro\s+acad[êe]mico|matr[íi]cula)\b[^\d\n]{0,15}(\d{5,12})\b",
    re.IGNORECASE,
)

# Só celular (o `9` inicial obrigatório). Fixo de 8 dígitos ficou de fora:
# `\d{4}[\s-]?\d{4}` casa "2024 2025" e qualquer par de números de 4 dígitos
# numa frase — exatamente o falso positivo que tornaria o alerta inútil.
#
# São duas formas, e nenhuma delas é "9 dígitos crus": `987654321` sozinho é
# indistinguível de um RA (foi assim que "matrícula 987654321" era contada
# como telefone também). O número precisa vir com DDD **ou** com o separador
# interno que o identifica como telefone.
_TELEFONE = re.compile(
    r"(?<!\d)(?:\(\d{2}\)\s*|\b\d{2}[\s.-]\s*)9\d{4}[\s.-]?\d{4}(?!\d)"  # com DDD
    r"|(?<!\d)9\d{4}[\s.-]\d{4}(?!\d)"                                    # sem DDD, com separador
)


# Senha colada no texto: a PALAVRA que a nomeia + um conector (`:`/`=`/`é`) ou
# aspas + o valor. O conector é o que separa a credencial de "esqueci minha
# senha" ou "a senha não funciona" — sem valor atribuído não há o que mascarar,
# e disparar nesses casos é o falso positivo que o cabeçalho proíbe.
#
# `valor` é o grupo mascarado; o grupo 1 (aspas) é a rede para "senha 'X'" sem
# conector. `\1` fecha as aspas quando abriram.
_SENHA = re.compile(
    r"""(?ix)
      \b (?: senha | password | pwd ) \b
      \s* (?: (?: é | eh | = | : ) \s* | (?=['"]) )
      (['"]?) (?P<valor> [^\s'"]{3,} ) \1
    """,
)


def _senha_e_credencial(match: re.Match) -> bool:
    """O valor após "senha:" parece uma credencial, e não uma palavra comum.

    "minha senha é fraca" / "senha nova" casam o padrão mas não revelam nada —
    só marca como credencial se o valor tiver dígito ou símbolo (`Aluno@2026`,
    `123456`) ou tiver vindo entre aspas. Senha só-letras e sem aspas é
    indistinguível de uma palavra da frase: falso negativo aceitável, falso
    positivo não (ver o cabeçalho).
    """
    if match.group(1):  # veio entre aspas
        return True
    return bool(re.search(r"[\d\W_]", _senha_nucleo(match)))


def _senha_nucleo(match: re.Match) -> str:
    """O valor da senha sem a pontuação final da frase.

    "senha é fraca," traz `,` no grupo (o `[^\\s'"]{3,}` é guloso), e `,` é `\\W`
    — sem esta limpeza toda palavra seguida de vírgula viraria "credencial", e o
    mascaramento comeria a vírgula junto. Entre aspas não se aplica: ali o
    delimitador é a própria aspa e o conteúdo inteiro é a senha.
    """
    valor = match.group("valor")
    return valor if match.group(1) else valor.rstrip(".,;:!?")


def _cpf_valido(digitos: str) -> bool:
    """Dígitos verificadores do CPF (módulo 11).

    Usado só no CPF SEM pontuação: `12345678901` também é um número de
    protocolo plausível, e sem esta checagem todo identificador interno de 11
    dígitos viraria alerta. Com pontuação (`123.456.789-01`) o formato já é o
    sinal e a validação não é aplicada — CPF digitado errado continua sendo
    dado pessoal.
    """
    if len(set(digitos)) == 1:  # 00000000000, 11111111111... passam no módulo 11
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * (tamanho + 1 - i) for i, d in enumerate(digitos[:tamanho]))
        verificador = (soma * 10) % 11 % 10
        if verificador != int(digitos[tamanho]):
            return False
    return True


def _cpf_e_identificador(match: re.Match) -> bool:
    bruto = match.group(0)
    digitos = re.sub(r"\D", "", bruto)
    tem_pontuacao = any(c in bruto for c in ".-")
    return tem_pontuacao or _cpf_valido(digitos)


def detectar(texto: str) -> list[str]:
    """Categorias de identificador presentes no texto, ordenadas e sem repetição.

    Devolve o RÓTULO, nunca o valor: é isto que vai para a telemetria
    (`Registro.pii`), onde guardar o dado em si seria justamente o problema.
    Lista vazia quando não há nada — o chamador decide se isso vira `None`.
    """
    if not texto:
        return []

    encontrados = []
    if any(_senha_e_credencial(m) for m in _SENHA.finditer(texto)):
        encontrados.append("senha")
    if _EMAIL.search(texto):
        encontrados.append("email")
    if any(_cpf_e_identificador(m) for m in _CPF.finditer(texto)):
        encontrados.append("cpf")
    if _RA.search(texto):
        encontrados.append("ra")
    if _TELEFONE.search(texto):
        encontrados.append("telefone")
    return sorted(encontrados)


def mascarar(texto: str | None) -> str | None:
    """Troca cada identificador por `[categoria]`, preservando o resto do texto.

    Passa em todo texto derivado da pergunta antes de ele ser persistido. O
    resultado continua legível para quem lê o relatório de lacunas — "acesso ao
    Canvas do [ra]" ainda diz qual documento falta indexar, que é o propósito
    do campo.

    `None` entra e sai como `None`: a etapa não aconteceu (ex.: `topico` num
    caminho sem chamada ao LLM) é diferente de "nada foi encontrado".
    """
    if not texto:
        return texto

    # Só o núcleo da senha vira `[senha]` — a palavra que nomeia ("senha é
    # [senha]") é contexto útil, e a pontuação da frase fica de fora.
    texto = _SENHA.sub(
        lambda m: m.group(0).replace(_senha_nucleo(m), "[senha]", 1)
        if _senha_e_credencial(m) else m.group(0),
        texto,
    )
    texto = _EMAIL.sub("[email]", texto)
    texto = _CPF.sub(lambda m: "[cpf]" if _cpf_e_identificador(m) else m.group(0), texto)
    # Preserva a palavra que nomeia o número (`RA [ra]`), que é contexto útil e
    # não é dado pessoal — só o grupo 1, o número, é substituído.
    texto = _RA.sub(lambda m: m.group(0).replace(m.group(1), "[ra]"), texto)
    texto = _TELEFONE.sub("[telefone]", texto)
    return texto
