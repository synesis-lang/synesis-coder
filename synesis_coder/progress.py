"""progress.py - Indicador de andamento para campanhas em lote.

Purpose:
    Uma campanha longa sem sinal de vida é indistinguível de uma campanha
    travada. O pesquisador cancela por não saber se falta muito — e perde o
    trabalho já feito. Este módulo emite uma linha periódica com posição,
    percentual, ritmo e ETA, de modo que a decisão de esperar ou interromper
    seja informada.

    Emite por `logger.info` (stderr), não por escrita direta no terminal: o
    stdout carrega o `.syn` no modo `plain`, `-q`/`-qq` silenciam
    naturalmente, e a saída continua legível sob `nohup`, `| tee` e CI — onde
    barras que reescrevem a linha viram lixo de escape.

    O throttling (`min_interval`) existe porque uma linha por registro numa
    campanha de 2.800 é ruído, não informação.

Components:
    - BatchProgress: acumula conclusões e decide quando emitir.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Intervalo mínimo entre linhas, em segundos. Abaixo disso a linha é suprimida
# (exceto a última, sempre emitida).
DEFAULT_MIN_INTERVAL = 5.0


def _plural(n: int, singular: str, plural: Optional[str] = None) -> str:
    """Forma correta para a contagem — "1 falha", não "1 falhas"."""
    if n == 1:
        return singular
    return plural if plural is not None else singular + "s"


def _counter(usage, name: str) -> int:
    """Lê um contador de `usage`, com 0 quando ausente ou não-numérico.

    Defensivo pelo mesmo motivo de `llm_client._int_attr`: nos testes o cliente
    costuma ser um MagicMock, cujo getattr devolve outro MagicMock — que não
    compara nem formata como número. O progresso deve degradar, nunca derrubar
    a campanha que está relatando.
    """
    if usage is None:
        return 0
    value = getattr(usage, name, 0)
    return value if isinstance(value, int) else 0


def format_eta(seconds: float) -> str:
    """Tempo restante estimado, legível: `~45s`, `~12min`, `~2h13m`."""
    if seconds < 60:
        return f"~{int(seconds)}s"
    if seconds < 3600:
        return f"~{int(seconds / 60)}min"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    return f"~{hours}h{minutes:02d}m"


def format_rate(per_second: float, unit: str = "reg") -> str:
    """Ritmo de processamento, na escala que produz número legível.

    Backends locais processam em minutos por unidade; APIs, em unidades por
    segundo. Uma escala fixa deixaria um dos dois ilegível (`0.0 reg/s` ou
    `3600 reg/h`).
    """
    if per_second >= 1.0:
        return f"{per_second:.1f} {unit}/s"
    per_minute = per_second * 60
    if per_minute >= 1.0:
        return f"{per_minute:.1f} {unit}/min"
    return f"{per_minute * 60:.1f} {unit}/h"


class BatchProgress:
    """Andamento de um lote, emitido periodicamente via logger.

    Thread-safe: os modos concorrentes concluem unidades em tarefas distintas
    (`asyncio.gather`/`as_completed` sobre `to_thread`), e `mark()` é chamado
    de qualquer uma delas.

    Example:
        progress = BatchProgress(total=len(entries), unit="ref")
        for ...:
            progress.mark(success=True)     # emite a cada 5s
        progress.finish()                   # linha final, sempre emitida
    """

    def __init__(
        self,
        total: int,
        unit: str = "reg",
        min_interval: float = DEFAULT_MIN_INTERVAL,
        label: Optional[str] = None,
        usage=None,
    ) -> None:
        """Inicializa o indicador.

        Args:
            total: Número de unidades a processar.
            unit: Rótulo curto da unidade (`ref`, `reg`, `chunk`, `item`).
            min_interval: Segundos mínimos entre linhas. 0 emite sempre.
            label: Prefixo opcional, útil quando um modo tem mais de uma etapa
                (ex.: "Codificando", "Revisando").
            usage: `TokenUsage` do cliente LLM. Quando informado, a linha passa
                a exibir correções e fallbacks acumulados — sinais que pedem
                ajuste DURANTE a campanha, não depois. Vê-los só no sumário
                final é tarde: numa campanha de horas, o ajuste que importa é
                o que se faz nos primeiros minutos.
        """
        self.total = total
        self.unit = unit
        self.min_interval = min_interval
        self.label = label
        self.usage = usage
        self.done = 0
        self.failed = 0
        self._start = time.monotonic()
        self._last_emit = self._start
        self._lock = threading.Lock()

    def mark(self, success: bool = True, detail: Optional[str] = None) -> None:
        """Registra uma unidade concluída e emite a linha se for hora.

        Args:
            success: False incrementa o contador de falhas.
            detail: Texto curto anexado à linha (ex.: o bibref corrente).
        """
        with self._lock:
            self.done += 1
            if not success:
                self.failed += 1
            now = time.monotonic()
            is_last = self.done >= self.total
            if not is_last and (now - self._last_emit) < self.min_interval:
                return
            self._last_emit = now
            line = self._render(now, detail)
        logger.info(line)

    def finish(self) -> None:
        """Emite a linha final, mesmo que o throttling a tivesse suprimido.

        Idempotente quando `mark()` já emitiu a última: repetir a mesma
        posição não confunde, e garante fechamento quando o total real ficou
        abaixo do previsto (falhas que não chamam `mark`).
        """
        with self._lock:
            if self.done == 0:
                return
            line = self._render(time.monotonic(), None)
        logger.info(line)

    def _render(self, now: float, detail: Optional[str]) -> str:
        elapsed = now - self._start
        width = len(str(self.total))
        pct = (self.done / self.total * 100) if self.total else 0.0

        parts = []
        if self.label:
            parts.append(self.label)
        parts.append(f"[{self.done:>{width}}/{self.total} | {pct:5.1f}%]")

        # Sinais de ajuste: só aparecem quando há o que ajustar. Uma campanha
        # limpa não carrega "0 falhas · 0 correções · 0 fallbacks".
        signals = []
        if self.failed:
            signals.append(f"{self.failed} {_plural(self.failed, 'falha')}")
        corrections = _counter(self.usage, "corrections")
        if corrections:
            signals.append(f"{corrections} {_plural(corrections, 'correção', 'correções')}")
        fallbacks = _counter(self.usage, "schema_fallbacks")
        if fallbacks:
            signals.append(f"{fallbacks} fallback{'s' if fallbacks > 1 else ''}")
        if signals:
            parts.append("· " + " · ".join(signals))

        if elapsed > 0 and self.done:
            rate = self.done / elapsed
            parts.append(f"| {format_rate(rate, self.unit)}")
            remaining = self.total - self.done
            # ETA abaixo de 1s é ruído: a campanha já acabou na prática.
            if remaining > 0 and rate > 0 and remaining / rate >= 1.0:
                parts.append(f"| ETA {format_eta(remaining / rate)}")

        if detail:
            parts.append(f"| {detail}")

        return " ".join(parts)
