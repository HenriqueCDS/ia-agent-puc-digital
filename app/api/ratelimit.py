"""Rate limit por consumidor + teto diário do processo (T2.3).

Em memória, de propósito: hoje a API roda em um processo só e a dependência
nova (Redis) custaria mais que o problema que resolve. O limite existe para
proteger a cota do Gemini e evitar que o buscador externo comece a devolver
429 — não é defesa contra atacante distribuído.

LIMITE CONHECIDO — com mais de um worker do uvicorn, cada processo tem o seu
contador, e os tetos efetivos viram `N * limite`. Quando isso passar a valer
(ver T4.3, que decide workers vs. RAM do modelo), o estado sai daqui para um
store compartilhado: `RateLimiter` já é a única coisa que precisa mudar, e
`get_rate_limiter()` é o único ponto de acesso.

Janela DESLIZANTE, não fixa: com janela fixa (contador que zera no minuto
cheio), 20/min permite 40 chamadas em 2 segundos em volta da virada — que é
exatamente o pico que o limite deveria conter.
"""

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from app.core.config import settings

JANELA_SEGUNDOS = 60


class RateLimitExcedido(Exception):
    """Vira 429 + `Retry-After` no handler de `app/api/errors.py`.

    `retry_after` em segundos é o que separa um cliente que espera o tempo
    certo de um que entra em loop de retry e piora o congestionamento.
    """

    def __init__(self, detalhe: str, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(detalhe)


class RateLimiter:
    def __init__(self, por_minuto: int, teto_diario: int):
        self.por_minuto = por_minuto
        self.teto_diario = teto_diario
        self._chamadas: dict[str, deque[float]] = defaultdict(deque)
        self._dia = self._hoje()
        self._total_do_dia = 0
        # As rotas são `def` (síncronas), então o FastAPI as executa no
        # threadpool: dois consumidores podem estar aqui de verdade ao mesmo
        # tempo. Sem o lock, `deque` e contador viram corrida.
        self._lock = threading.Lock()

    @staticmethod
    def _hoje() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _segundos_ate_amanha() -> int:
        agora = datetime.now(timezone.utc)
        virada = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int((virada - agora).total_seconds())

    def verificar(self, consumidor: str) -> None:
        """Registra uma chamada ou levanta `RateLimitExcedido`.

        O teto diário é checado ANTES do limite por consumidor: estourado o
        orçamento do dia, o `Retry-After` correto é o da virada do dia, não o
        dos 60 segundos da janela.
        """
        agora = time.monotonic()
        with self._lock:
            dia = self._hoje()
            if dia != self._dia:
                self._dia, self._total_do_dia = dia, 0

            if self._total_do_dia >= self.teto_diario:
                raise RateLimitExcedido(
                    f"Teto diário do serviço ({self.teto_diario} chamadas) atingido.",
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

    def esquecer(self, consumidor: str) -> None:
        """Só para teste: zera a janela de um consumidor."""
        with self._lock:
            self._chamadas.pop(consumidor, None)


_limiter: RateLimiter | None = None
_criacao = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Instância única do processo. Preguiçosa para ler `settings` já carregado
    (e para que um teste que muda os limites via monkeypatch consiga resetar
    com `reset_rate_limiter()`)."""
    global _limiter
    with _criacao:
        if _limiter is None:
            _limiter = RateLimiter(
                por_minuto=settings.rate_limit_por_minuto,
                teto_diario=settings.rate_limit_diario_global,
            )
        return _limiter


def reset_rate_limiter() -> None:
    """Descarta o estado acumulado. Usado pelos testes e por `create_app()`,
    para que dois apps criados no mesmo processo não dividam contadores."""
    global _limiter
    with _criacao:
        _limiter = None
