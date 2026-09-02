"""Testes da janela deslizante e do teto diário (T2.3).

A borda HTTP (401/429/`Retry-After`) está em `tests/test_api.py`; aqui fica a
aritmética da janela, que é a parte sutil e que não precisa de um app inteiro
para ser exercida.

O relógio é dublado (`time.monotonic`) em vez de esperar de verdade: um teste
de janela de 60s que dorme é um teste que ninguém roda.
"""

import logging

import pytest

from app.api import ratelimit
from app.api.ratelimit import RateLimiter, RateLimitExcedido, RedisRateLimiter


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


# --- Backend Redis (INF-10) -------------------------------------------------
#
# Mesma aritmética dos testes acima, agora contra um Redis dublado (fakeredis).
# O ponto que só este backend prova: dois limiters (= dois workers) no mesmo
# Redis NÃO somam N×limite, que é o bug que INF-10 fecha.


@pytest.fixture
def redis_fake():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def relogio_parede(monkeypatch):
    """Relógio de parede controlado — o backend Redis usa `time.time()`, não
    `monotonic` (pode haver mais de um host)."""
    agora = {"t": 1_000_000.0}
    monkeypatch.setattr(ratelimit.time, "time", lambda: agora["t"])

    def avancar(segundos):
        agora["t"] += segundos

    return avancar


def _limiter_redis(redis_fake, **kw):
    kw.setdefault("por_minuto", 3)
    kw.setdefault("teto_diario", 100)
    return RedisRateLimiter("redis://dummy", cliente=redis_fake, **kw)


def _chave_global():
    return f"rl:dia:global:{ratelimit._dia_utc()}"


def test_redis_permite_ate_o_limite_e_bloqueia_a_seguinte(redis_fake, relogio_parede):
    limiter = _limiter_redis(redis_fake, por_minuto=3)

    for _ in range(3):
        limiter.verificar("ava")

    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")


def test_redis_a_vaga_reabre_deslizante(redis_fake, relogio_parede):
    limiter = _limiter_redis(redis_fake, por_minuto=2)
    limiter.verificar("ava")
    relogio_parede(30)
    limiter.verificar("ava")

    relogio_parede(29)  # 59s da primeira: ainda cheio
    with pytest.raises(RateLimitExcedido):
        limiter.verificar("ava")

    relogio_parede(2)  # 61s da primeira: liberou a vaga
    limiter.verificar("ava")


def test_redis_chamada_recusada_nao_consome_vaga_nem_contador(redis_fake, relogio_parede):
    """O `desfazer` reverte o ZADD e os INCR quando a checagem por minuto
    rejeita — senão a rejeição gastaria o teto diário."""
    limiter = _limiter_redis(redis_fake, por_minuto=1, teto_diario=100)
    limiter.verificar("ava")

    for _ in range(3):
        with pytest.raises(RateLimitExcedido):
            limiter.verificar("ava")

    assert int(redis_fake.get(_chave_global())) == 1  # só a 1ª contou

    relogio_parede(61)
    limiter.verificar("ava")  # a 1ª saiu da janela; a vaga reabriu


def test_redis_teto_diario_global_soma_todos_os_consumidores(redis_fake, relogio_parede):
    limiter = _limiter_redis(redis_fake, por_minuto=100, teto_diario=2)
    limiter.verificar("ava")
    limiter.verificar("portal")

    with pytest.raises(RateLimitExcedido) as erro:
        limiter.verificar("outro")

    assert "diário" in str(erro.value)
    assert erro.value.retry_after > 60  # a virada do dia, não os 60s da janela


def test_redis_teto_diario_por_consumidor(redis_fake, relogio_parede):
    limiter = _limiter_redis(
        redis_fake, por_minuto=100, teto_diario=100, tetos_diarios={"demo": 1}
    )
    limiter.verificar("demo")

    with pytest.raises(RateLimitExcedido):
        limiter.verificar("demo")

    limiter.verificar("ava")  # outro consumidor não herda o teto da demo


def test_redis_rejeicao_por_teto_proprio_devolve_o_global(redis_fake, relogio_parede):
    """A chamada barrada pelo teto do consumidor não pode ter contado no teto
    global — senão o limite de custo vazaria pelas rejeições."""
    limiter = _limiter_redis(
        redis_fake, por_minuto=100, teto_diario=100, tetos_diarios={"demo": 1}
    )
    limiter.verificar("demo")
    with pytest.raises(RateLimitExcedido):
        limiter.verificar("demo")

    assert int(redis_fake.get(_chave_global())) == 1


def test_redis_dois_workers_nao_somam_o_dobro_do_limite(redis_fake, relogio_parede):
    """INF-10: dois limiters (dois processos) apontando para o mesmo Redis
    compartilham o balde — o teto de 2/min continua 2/min, não 4/min."""
    worker_a = _limiter_redis(redis_fake, por_minuto=2)
    worker_b = _limiter_redis(redis_fake, por_minuto=2)

    worker_a.verificar("ava")
    worker_b.verificar("ava")

    with pytest.raises(RateLimitExcedido):
        worker_a.verificar("ava")
    with pytest.raises(RateLimitExcedido):
        worker_b.verificar("ava")


def test_redis_fora_do_ar_libera_a_requisicao_com_warning(caplog):
    class RedisMorto:
        def incr(self, *a, **k):
            raise ratelimit.redis.ConnectionError("conexão recusada")

    limiter = RedisRateLimiter(
        "redis://dummy", por_minuto=1, teto_diario=1, cliente=RedisMorto()
    )

    with caplog.at_level(logging.WARNING):
        limiter.verificar("ava")  # não levanta
        limiter.verificar("ava")  # nem além do teto — o Redis é que decide, e ele caiu

    assert "Redis indisponível" in caplog.text


def test_redis_erro_de_logica_nao_e_engolido(redis_fake, relogio_parede, monkeypatch):
    """Fail-open é só para `RedisError`. Um bug nosso tem que propagar (§8 do
    perfil: erro do pedido/lógica propaga, erro do provedor faz fallback)."""
    limiter = _limiter_redis(redis_fake)

    def incr_bugado(*a, **k):
        raise KeyError("bug nosso")

    monkeypatch.setattr(limiter._r, "incr", incr_bugado)

    with pytest.raises(KeyError):
        limiter.verificar("ava")


def test_get_rate_limiter_escolhe_o_backend_pela_redis_url(monkeypatch):
    monkeypatch.setattr(ratelimit.settings, "redis_url", "redis://localhost:6379/0")
    ratelimit.reset_rate_limiter()
    try:
        assert isinstance(ratelimit.get_rate_limiter(), RedisRateLimiter)
    finally:
        ratelimit.reset_rate_limiter()

    monkeypatch.setattr(ratelimit.settings, "redis_url", "")
    ratelimit.reset_rate_limiter()
    try:
        assert isinstance(ratelimit.get_rate_limiter(), RateLimiter)
    finally:
        ratelimit.reset_rate_limiter()
