"""
token_usage.py - Acumulador thread-safe de tokens consumidos por chamadas LLM.

Purpose:
    Rastreia totais de tokens de input/output ao longo de multiplas chamadas
    LLM numa mesma execucao. Projetado para ser mantido como atributo interno
    do LLMClient e lido pelos modos para exibicao no formato verbose.

Components:
    - TokenUsage: dataclass com acumulacao thread-safe e formatacao de saida.

Dependencies:
    - threading: lock para seguranca em modos concorrentes (abstract, document, ontology)

Example:
    from synesis_coder.token_usage import TokenUsage

    usage = TokenUsage()
    usage.record(input_tok=1200, output_tok=340)
    usage.record(input_tok=980, output_tok=210, is_correction=True)
    print(usage.summary_line())
    # tokens: in 2,180 | out 550 | total 2,730 | calls 2 | corrections 1
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TokenUsage:
    """Acumula totais de tokens de input/output ao longo de multiplas chamadas LLM.

    Thread-safe: os modos concorrentes (abstract, document, ontology) executam
    chamadas LLM em threads separadas via asyncio.to_thread(). O lock garante
    que as acumulacoes sejam atomicas.

    Attributes:
        input_tokens: Total de tokens de entrada (prompt).
        output_tokens: Total de tokens de saida (completion).
        api_calls: Numero total de chamadas a API.
        corrections: Numero de chamadas de correcao (fix/fix_async).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    corrections: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def record(
        self,
        input_tok: int,
        output_tok: int,
        is_correction: bool = False,
    ) -> None:
        """Registra tokens de uma chamada LLM.

        Args:
            input_tok: Tokens de entrada desta chamada.
            output_tok: Tokens de saida desta chamada.
            is_correction: True se for uma chamada de correcao (fix/fix_async).
        """
        with self._lock:
            self.input_tokens += input_tok
            self.output_tokens += output_tok
            self.api_calls += 1
            if is_correction:
                self.corrections += 1

    @property
    def total_tokens(self) -> int:
        """Soma de input_tokens + output_tokens."""
        return self.input_tokens + self.output_tokens

    def summary_line(self) -> str:
        """Linha formatada para exibicao no terminal (formato verbose).

        Returns:
            String no formato:
            "tokens: in X,XXX | out X,XXX | total X,XXX | calls N"
            Com "| corrections N" adicionado quando corrections > 0.
        """
        parts = [
            f"tokens: in {self.input_tokens:,}",
            f"out {self.output_tokens:,}",
            f"total {self.total_tokens:,}",
            f"calls {self.api_calls}",
        ]
        if self.corrections:
            parts.append(f"corrections {self.corrections}")
        return " | ".join(parts)

    def reset(self) -> None:
        """Reinicia todos os contadores."""
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.api_calls = 0
            self.corrections = 0
