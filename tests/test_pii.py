"""Detecção/mascaramento de identificador pessoal (T3.4).

Metade destes testes é sobre FALSO POSITIVO, não sobre detecção: um alerta de
LGPD que dispara em número de protocolo e em ano vira ruído e é desligado em
duas semanas — aí o vazamento de verdade passa junto.
"""

import pytest

from app.core import pii

# CPF gerado só para teste, com dígitos verificadores válidos.
CPF_VALIDO = "52998224725"


# --- detecção ------------------------------------------------------------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("meu RA é 12345678 e não consigo entrar", ["ra"]),
        ("matrícula 987654321, como faço?", ["ra"]),
        ("registro acadêmico: 20231234", ["ra"]),
        ("meu cpf é 529.982.247-25", ["cpf"]),
        (f"cpf {CPF_VALIDO} bloqueado", ["cpf"]),
        ("me responde em aluno@puc-campinas.edu.br", ["email"]),
        ("meu whatsapp é (19) 99123-4567", ["telefone"]),
        ("19 991234567 é meu contato", ["telefone"]),
        ("RA 12345678, cpf 529.982.247-25, email a@b.com", ["cpf", "email", "ra"]),
        # senha (PII-2): a palavra + conector + um valor com cara de credencial.
        ("minha senha é Aluno@2026 e não entra", ["senha"]),
        ("senha: 123456", ["senha"]),
        ("password=Trocar#1", ["senha"]),
        ("tentei com a senha 'Puc2026!' sem sucesso", ["senha"]),
        ("email a@b.com e minha senha é Aluno@2026", ["email", "senha"]),
        ("como envio uma atividade no Canvas?", []),
        ("", []),
    ],
)
def test_detectar(texto, esperado):
    assert pii.detectar(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        # sem valor atribuído: reclamação, não divulgação de credencial.
        "esqueci minha senha, como recupero?",
        "a senha não funciona mais",
        "preciso trocar minha senha nova",
        "minha senha é fraca, quero uma dica",
    ],
)
def test_senha_sem_valor_atribuido_nao_dispara(texto):
    assert pii.detectar(texto) == []


@pytest.mark.parametrize(
    "texto",
    [
        # 11 dígitos que NÃO são CPF válido: protocolo, id interno, sequência.
        "protocolo 12345678901 aberto ontem",
        "o código é 11111111111",
        # Dois números de 4 dígitos numa frase — o falso positivo que fez o
        # padrão de telefone exigir o 9 do celular.
        "o calendário 2024 2025 já saiu?",
        "a turma 1234 tem 5678 alunos",
        # Números curtos e datas.
        "a aula é dia 12/03 às 19h30",
        "capítulo 3, página 128",
    ],
)
def test_nao_dispara_em_numero_que_nao_e_identificador(texto):
    assert pii.detectar(texto) == []


def test_cpf_com_pontuacao_dispara_mesmo_com_digito_verificador_errado():
    # O formato já é o sinal: CPF digitado errado continua sendo dado pessoal.
    assert pii.detectar("meu cpf é 123.456.789-00") == ["cpf"]


def test_ra_de_9_digitos_nao_e_contado_tambem_como_telefone():
    # Regressão: `987654321` casa `9` + 8 dígitos, e a primeira versão do padrão
    # marcava todo RA de 9 dígitos como telefone junto. Um alerta que reporta
    # duas categorias onde há uma corrói a confiança no relatório inteiro.
    assert pii.detectar("matrícula 987654321") == ["ra"]


def test_celular_sem_ddd_precisa_do_separador():
    assert pii.detectar("meu número é 99123-4567") == ["telefone"]


def test_numero_de_8_digitos_sem_a_palavra_ra_nao_e_ra():
    # Exigir o rótulo é a decisão que troca recall por precisão (ver pii.py):
    # 8 dígitos soltos são indistinguíveis de protocolo ou código de disciplina.
    assert pii.detectar("o código 20231234 aparece na tela") == []


# --- mascaramento --------------------------------------------------------


def test_mascarar_preserva_o_texto_em_volta():
    # O tópico mascarado ainda precisa dizer QUAL documento falta indexar —
    # é para isso que ele existe no relatório de lacunas.
    assert pii.mascarar("acesso ao Canvas do RA 12345678") == "acesso ao Canvas do RA [ra]"


def test_mascarar_cada_categoria():
    texto = "cpf 529.982.247-25, email a@b.com, tel (19) 99123-4567"
    assert pii.mascarar(texto) == "cpf [cpf], email [email], tel [telefone]"


def test_email_com_digitos_nao_vira_cpf_pela_metade():
    # E-mail é mascarado ANTES do CPF de propósito: sem essa ordem, o padrão de
    # CPF casaria dentro do endereço e o resultado sairia truncado.
    assert pii.mascarar(f"escreva para ra{CPF_VALIDO}@puc.br") == "escreva para [email]"


def test_mascarar_senha_preserva_a_palavra_e_troca_so_o_valor():
    assert pii.mascarar("minha senha é Aluno@2026, não entra") == (
        "minha senha é [senha], não entra"
    )
    assert pii.mascarar("senha: 123456") == "senha: [senha]"
    assert pii.mascarar("tentei com a senha 'Puc2026!'") == "tentei com a senha '[senha]'"


def test_mascarar_senha_com_email_como_valor_nao_vira_email():
    # senha é mascarada ANTES do e-mail: "senha é joao@x.com" é credencial,
    # não um endereço para contato.
    assert pii.mascarar("minha senha é joao@x.com") == "minha senha é [senha]"


def test_mascarar_senha_sem_valor_de_credencial_nao_muda_nada():
    texto = "esqueci minha senha, como recupero?"
    assert pii.mascarar(texto) == texto


def test_mascarar_texto_sem_pii_nao_muda_nada():
    texto = "como envio uma atividade no Canvas?"
    assert pii.mascarar(texto) == texto


def test_mascarar_none_continua_none():
    # `None` = "a etapa não aconteceu" (ex.: topico sem chamada ao LLM), que é
    # diferente de "nada foi encontrado".
    assert pii.mascarar(None) is None
