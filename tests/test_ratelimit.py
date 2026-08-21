"""Testes da janela deslizante e do teto diário (T2.3).

A borda HTTP (401/429/`Retry-After`) está em `tests/test_api.py`; aqui fica a
aritmética da janela, que é a parte sutil e que não precisa de um app inteiro
para ser exercida.

O relógio é dublado (`time.monotonic`) em vez de esperar de verdade: um teste
de janela de 60s que dorme é um teste que ninguém roda.
"""

import pytest

from app.api import ratelimit
from app.api.ratelimit import RateLimiter, RateLimitExcedido


@pytest.fixture
def relogio(monkeypatch):
    """Relógio controlado. `avancar(s)` move o tempo do limiter."""
    agora = {"t": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: agora["t"])

    def avancar(segundos):
        agora["t"] += segundos

    return avancar


def test_permite_ate_o_limite_e_bloqueia_a_seguinte(relogio):
    limiter = RateLimiter(por_minuto=3, teto_diario=100)

    for _ in range(3):
        limiter.verificar("ava")

    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")


def test_a_vaga_reabre_quando_a_chamada_mais_antiga_sai_da_janela(relogio):
    """É o que "deslizante" significa: a vaga não espera o minuto cheio, e sim
    a chamada que a ocupou completar 60s."""
    limiter = RateLimiter(por_minuto=2, teto_diario=100)
    limiter.verificar("ava")
    relogio(30)
    limiter.verificar("ava")

    relogio(29)  # 59s da primeira chamada: ainda cheio
    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")

    relogio(2)  # 61s da primeira: ela saiu da janela e liberou a vaga
    limiter.verificar("ava")


def test_janela_fixa_seria_o_dobro_do_limite_na_virada(relogio):
    """Regressão do motivo de a janela ser deslizante: com contador que zera no
    minuto cheio, 2/min aceitaria 4 chamadas em 2 segundos em volta da virada."""
    limiter = RateLimiter(por_minuto=2, teto_diario=100)
    relogio(59)
    limiter.verificar("ava")
    limiter.verificar("ava")
    relogio(2)  # "novo minuto" para uma janela fixa

    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")


def test_retry_after_e_o_tempo_ate_a_vaga_abrir(relogio):
    limiter = RateLimiter(por_minuto=1, teto_diario=100)
    limiter.verificar("ava")
    relogio(20)

    with pytest.raises(RateLimitExcedido) as erro:
        limiter.verificar("ava")

    # Faltam 40s para a chamada sair da janela; +1 de arredondamento para cima,
    # porque devolver 0 convidaria o cliente ao retry imediato.
    assert erro.value.retry_after == 41


def test_consumidores_tem_baldes_separados(relogio):
    limiter = RateLimiter(por_minuto=1, teto_diario=100)
    limiter.verificar("ava")

    limiter.verificar("portal")  # não herda o balde do "ava"

    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")


def test_teto_diario_vale_para_a_soma_de_todos_os_consumidores(relogio):
    """O teto diário é o limite de CUSTO: várias integrações bem-comportadas
    ainda somam uma fatura, e é a soma que ele protege."""
    limiter = RateLimiter(por_minuto=100, teto_diario=2)
    limiter.verificar("ava")
    limiter.verificar("portal")

    with pytest.raises(RateLimitExcedido) as erro:
        limiter.verificar("outro")

    assert "diário" in str(erro.value)
    # Esperar 60s não resolve: o que reabre é a virada do dia.
    assert erro.value.retry_after > 60


def test_teto_diario_e_checado_antes_do_limite_por_minuto(relogio):
    """Com os dois estourados, o `Retry-After` correto é o do dia — o cliente
    que espera 60s e volta só vai tomar outro 429."""
    limiter = RateLimiter(por_minuto=1, teto_diario=1)
    limiter.verificar("ava")

    with pytest.raises(RateLimitExcedido) as erro:
        limiter.verificar("ava")

    assert erro.value.retry_after > 60


def test_contador_do_dia_zera_na_virada(relogio, monkeypatch):
    limiter = RateLimiter(por_minuto=100, teto_diario=1)
    limiter.verificar("ava")
    monkeypatch.setattr(RateLimiter, "_hoje", staticmethod(lambda: "2026-08-22"))

    limiter.verificar("ava")  # dia novo, orçamento novo


def test_get_rate_limiter_e_o_mesmo_do_processo():
    ratelimit.reset_rate_limiter()

    assert ratelimit.get_rate_limiter() is ratelimit.get_rate_limiter()

    ratelimit.reset_rate_limiter()
    assert ratelimit.get_rate_limiter() is not None
