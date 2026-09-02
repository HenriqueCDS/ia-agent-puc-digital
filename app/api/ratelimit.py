"""Rate limit por consumidor + teto diário do processo (T2.3).

Dois backends, escolhidos por `REDIS_URL` em `get_rate_limiter()`:

- **em memória** (`RateLimiter`, o padrão): contadores em estruturas Python do
  processo. Correto enquanto a API roda em UM worker. Com mais de um worker do
  uvicorn cada processo tem o seu contador e os tetos efetivos viram
  `N * limite` (INF-10) — é o motivo de o outro backend existir.
- **Redis** (`RedisRateLimiter`, quando `REDIS_URL` está setado): os mesmos três
  tetos, mas contados em chaves compartilhadas — `INCR` para os diários,
  sorted set + pipeline `MULTI/EXEC` para a janela deslizante. Todos os workers
  (e todas as réplicas) somam no mesmo lugar.

O limite existe para proteger a cota do Gemini e evitar que o buscador externo
comece a devolver 429 — não é defesa contra atacante distribuído.

Janela DESLIZANTE, não fixa: com janela fixa (contador que zera no minuto
cheio), 20/min permite 40 chamadas em 2 segundos em volta da virada — que é
exatamente o pico que o limite deveria conter.

DEGRADAÇÃO — se o Redis estiver inacessível, `RedisRateLimiter.verificar` LIBERA
a requisição e loga um WARNING (perfil §7: falha de dependência não derruba o
caminho principal). Durante um incidente de Redis os tetos não valem; a
alternativa — recusar tudo ou 500 — transformaria uma instabilidade do Redis em
indisponibilidade do agente. Um bug nosso (≠ `RedisError`) continua propagando.

Na rejeição sob concorrência, o backend Redis pode recusar uma chamada a mais na
fronteira exata do limite (ele conta com `INCR`/`ZADD` e desfaz quando a checagem
seguinte falha, e outra chamada pode ver o contador inflado nesse intervalo). O
desvio é SEMPRE no sentido seguro (recusa a mais, nunca admite a mais) e limitado
ao número de chamadas concorrentes na fronteira.
"""

import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

JANELA_SEGUNDOS = 60


class RateLimitExcedido(Exception):
    """Vira 429 + `Retry-After` no handler de `app/api/errors.py`.

    `retry_after` em segundos é o que separa um cliente que espera o tempo
    certo de um que entra em loop de retry e piora o congestionamento.
    """

    def __init__(self, detalhe: str, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(detalhe)


def _dia_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _segundos_ate_amanha() -> int:
    agora = datetime.now(timezone.utc)
    virada = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((virada - agora).total_seconds())


class Limiter(Protocol):
    """A interface que `deps.consumidor_com_rate_limit` consome — o único ponto
    de acoplamento com a borda HTTP. Os dois backends a implementam."""

    por_minuto: int

    def verificar(self, consumidor: str) -> None: ...

    def esquecer(self, consumidor: str) -> None: ...


class RateLimiter:
    """Backend em memória. Um contador por processo — ver o aviso do módulo."""

    def __init__(
        self,
        por_minuto: int,
        teto_diario: int,
        tetos_diarios: dict[str, int] | None = None,
    ):
        self.por_minuto = por_minuto
        self.teto_diario = teto_diario
        # Teto diário próprio de alguns consumidores (T3.1). Quem não está aqui
        # responde só ao teto global.
        self.tetos_diarios = tetos_diarios or {}
        self._chamadas: dict[str, deque[float]] = defaultdict(deque)
        self._dia = self._hoje()
        self._total_do_dia = 0
        self._total_por_consumidor: dict[str, int] = defaultdict(int)
        # As rotas são `def` (síncronas), então o FastAPI as executa no
        # threadpool: dois consumidores podem estar aqui de verdade ao mesmo
        # tempo. Sem o lock, `deque` e contador viram corrida.
        self._lock = threading.Lock()

    @staticmethod
    def _hoje() -> str:
        return _dia_utc()

    @staticmethod
    def _segundos_ate_amanha() -> int:
        return _segundos_ate_amanha()

    def verificar(self, consumidor: str) -> None:
        """Registra uma chamada ou levanta `RateLimitExcedido`.

        Os tetos DIÁRIOS são checados antes do limite por minuto: estourado o
        orçamento do dia, o `Retry-After` correto é o da virada do dia, não o
        dos 60 segundos da janela. Entre os dois diários, o global vem primeiro
        — é o limite de custo do serviço inteiro, e nenhum consumidor deve
        conseguir passar dele por ter um teto próprio maior.
        """
        agora = time.monotonic()
        with self._lock:
            dia = self._hoje()
            if dia != self._dia:
                self._dia, self._total_do_dia = dia, 0
                self._total_por_consumidor.clear()

            if self._total_do_dia >= self.teto_diario:
                raise RateLimitExcedido(
                    f"Teto diário do serviço ({self.teto_diario} chamadas) atingido.",
                    self._segundos_ate_amanha(),
                )

            teto_proprio = self.tetos_diarios.get(consumidor)
            if teto_proprio is not None and self._total_por_consumidor[consumidor] >= teto_proprio:
                # Não diz o nome do consumidor na mensagem: ela chega ao
                # navegador na demo, e o rótulo interno da integração não é
                # informação que precise sair daqui.
                raise RateLimitExcedido(
                    f"Teto diário desta integração ({teto_proprio} chamadas) atingido.",
                    self._segundos_ate_amanha(),
                )

            janela = self._chamadas[consumidor]
            while janela and agora - janela[0] >= JANELA_SEGUNDOS:
                janela.popleft()

            if len(janela) >= self.por_minuto:
                # A vaga só abre quando a chamada mais antiga sai da janela.
                espera = JANELA_SEGUNDOS - (agora - janela[0])
                raise RateLimitExcedido(
                    f"Limite de {self.por_minuto} chamadas por minuto atingido.",
                    espera + 1,  # arredonda para cima: devolver 0 convida ao retry imediato
                )

            janela.append(agora)
            self._total_do_dia += 1
            self._total_por_consumidor[consumidor] += 1

    def esquecer(self, consumidor: str) -> None:
        """Só para teste: zera a janela de um consumidor."""
        with self._lock:
            self._chamadas.pop(consumidor, None)


class RedisRateLimiter:
    """Backend compartilhado (INF-10). Mesma semântica de `RateLimiter`, mas os
    contadores vivem no Redis — todos os workers somam no mesmo lugar.

    Nunca conecta no `__init__` (redis-py conecta preguiçosamente): um Redis
    fora do ar no boot não impede a API de subir; cada `verificar` tenta e, se o
    Redis não responder, libera a requisição (ver o aviso do módulo).
    """

    _PREFIXO = "rl"

    def __init__(
        self,
        url: str,
        por_minuto: int,
        teto_diario: int,
        tetos_diarios: dict[str, int] | None = None,
        cliente=None,
    ):
        self.por_minuto = por_minuto
        self.teto_diario = teto_diario
        self.tetos_diarios = tetos_diarios or {}
        # `cliente` injetável para o dublê dos testes (fakeredis). Timeouts
        # curtos no cliente real: um Redis morto tem que FALHAR rápido para o
        # fail-open acontecer, não segurar a request até o timeout do cliente.
        self._r = cliente or redis.Redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
        )

    def _k_janela(self, consumidor: str) -> str:
        return f"{self._PREFIXO}:min:{consumidor}"

    def _k_global(self, dia: str) -> str:
        return f"{self._PREFIXO}:dia:global:{dia}"

    def _k_consumidor(self, consumidor: str, dia: str) -> str:
        return f"{self._PREFIXO}:dia:c:{consumidor}:{dia}"

    def verificar(self, consumidor: str) -> None:
        try:
            self._verificar(consumidor)
        except redis.RedisError as exc:
            # Fail-open: não conta e não bloqueia. `RateLimitExcedido` NÃO é
            # `RedisError`, então uma rejeição legítima continua subindo; um
            # bug nosso (KeyError, etc.) também — e vira 500, que é o certo.
            logger.warning(
                "rate limit: Redis indisponível (%s: %s); liberando a requisição sem contar",
                type(exc).__name__,
                exc,
            )

    def _verificar(self, consumidor: str) -> None:
        agora = time.time()  # relógio de parede: pode haver mais de um host
        dia = _dia_utc()
        ttl_dia = _segundos_ate_amanha()
        k_global = self._k_global(dia)
        k_consumidor = self._k_consumidor(consumidor, dia)
        k_janela = self._k_janela(consumidor)
        teto_proprio = self.tetos_diarios.get(consumidor)

        # `desfazer` reverte os incrementos já aplicados quando uma checagem
        # POSTERIOR rejeita — a ordem (global → consumidor → minuto) é a mesma
        # do backend em memória, mas lá nada é escrito até tudo passar.
        desfazer: list = []
        try:
            total_global = self._r.incr(k_global)
            desfazer.append(lambda: self._r.decr(k_global))
            if total_global == 1:
                self._r.expire(k_global, ttl_dia)
            if total_global > self.teto_diario:
                raise RateLimitExcedido(
                    f"Teto diário do serviço ({self.teto_diario} chamadas) atingido.",
                    ttl_dia,
                )

            if teto_proprio is not None:
                total_consumidor = self._r.incr(k_consumidor)
                desfazer.append(lambda: self._r.decr(k_consumidor))
                if total_consumidor == 1:
                    self._r.expire(k_consumidor, ttl_dia)
                if total_consumidor > teto_proprio:
                    raise RateLimitExcedido(
                        f"Teto diário desta integração ({teto_proprio} chamadas) atingido.",
                        ttl_dia,
                    )

            membro = f"{agora:.6f}:{uuid.uuid4().hex}"
            pipe = self._r.pipeline()  # MULTI/EXEC: as 4 rodam como bloco atômico
            pipe.zremrangebyscore(k_janela, 0, agora - JANELA_SEGUNDOS)
            pipe.zadd(k_janela, {membro: agora})
            pipe.zcard(k_janela)
            pipe.expire(k_janela, JANELA_SEGUNDOS + 1)
            _, _, atual, _ = pipe.execute()
            if atual > self.por_minuto:
                desfazer.append(lambda: self._r.zrem(k_janela, membro))
                mais_antiga = self._r.zrange(k_janela, 0, 0, withscores=True)
                espera = (
                    JANELA_SEGUNDOS - (agora - mais_antiga[0][1])
                    if mais_antiga
                    else JANELA_SEGUNDOS
                )
                raise RateLimitExcedido(
                    f"Limite de {self.por_minuto} chamadas por minuto atingido.",
                    espera + 1,
                )
        except RateLimitExcedido:
            for reverter in reversed(desfazer):
                try:
                    reverter()
                except redis.RedisError:  # pragma: no cover - o Redis caiu no meio
                    pass
            raise

    def esquecer(self, consumidor: str) -> None:
        """Só para teste / manutenção: zera a janela de um consumidor."""
        try:
            self._r.delete(self._k_janela(consumidor))
        except redis.RedisError:  # pragma: no cover
            pass


_limiter: Limiter | None = None
_criacao = threading.Lock()


def _construir_limiter() -> Limiter:
    comum = dict(
        por_minuto=settings.rate_limit_por_minuto,
        teto_diario=settings.rate_limit_diario_global,
        tetos_diarios=settings.tetos_diarios_por_consumidor,
    )
    if settings.redis_url:
        logger.info(
            "rate limit: contadores no Redis (%s)", urlsplit(settings.redis_url).hostname or "?"
        )
        return RedisRateLimiter(settings.redis_url, **comum)
    return RateLimiter(**comum)


def get_rate_limiter() -> Limiter:
    """Instância única do processo. Preguiçosa para ler `settings` já carregado
    (e para que um teste que muda os limites via monkeypatch consiga resetar
    com `reset_rate_limiter()`)."""
    global _limiter
    with _criacao:
        if _limiter is None:
            _limiter = _construir_limiter()
        return _limiter


def reset_rate_limiter() -> None:
    """Descarta o estado acumulado. Usado pelos testes e por `create_app()`,
    para que dois apps criados no mesmo processo não dividam contadores.

    Com o backend Redis não há estado local para descartar, mas o reset ainda
    força reler `settings` (limites trocados por monkeypatch em teste)."""
    global _limiter
    with _criacao:
        _limiter = None
