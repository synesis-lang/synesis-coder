"""
token_usage.py - Acumulador thread-safe de tokens consumidos por chamadas LLM.

Purpose:
    Rastreia totais de tokens de input/output ao longo de multiplas chamadas
    LLM numa mesma execucao. Projetado para ser mantido como atributo interno
    do LLMClient e lido pelos modos para exibicao no formato verbose.

    Tambem acumula a atividade de prompt caching (tokens escritos no cache e
    lidos dele), sem a qual nao ha como saber se o cache esta funcionando nem
    quanto ele economiza — ver "Semantica de input_tokens" abaixo.

Components:
    - TokenUsage: dataclass com acumulacao thread-safe e formatacao de saida.

Dependencies:
    - threading: lock para seguranca em modos concorrentes (abstract, document, ontology)

Semantica de input_tokens (difere por backend — CUIDADO):
    - Anthropic: `usage.input_tokens` e apenas o RESTO nao-cacheado. O total do
      prompt e input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
      Os tres campos sao disjuntos e devem ser somados para obter o total.
    - OpenAI-compat (inclui OpenRouter): `usage.prompt_tokens` ja e o TOTAL, e
      `prompt_tokens_details.cached_tokens` e um SUBCONJUNTO dele.

    Consequencia: `input_tokens` neste acumulador guarda o valor cru de cada
    backend, e `cache_*_tokens` sao registrados a parte. Use `total_tokens`
    (que trata a diferenca) em vez de somar os campos manualmente.

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
        input_tokens: Tokens de entrada. No backend Anthropic e o resto
            NAO-cacheado; no OpenAI-compat e o total do prompt (ver docstring
            do modulo).
        output_tokens: Total de tokens de saida (completion).
        cache_write_tokens: Tokens gravados no cache (Anthropic:
            cache_creation_input_tokens; OpenAI-compat: cache_write_tokens).
            Cobrados com premio (~1.25x na Anthropic).
        cache_read_tokens: Tokens servidos do cache (Anthropic:
            cache_read_input_tokens; OpenAI-compat: cached_tokens). Cobrados
            com desconto (~0.1x na Anthropic, ~0.25-0.5x na OpenAI).
        api_calls: Numero total de chamadas a API.
        corrections: Numero de chamadas de correcao (fix/fix_async).
        schema_fallbacks: Numero de vezes que o caminho JSON foi abandonado e a
            geracao caiu para texto livre, perdendo as garantias do schema.
            Sem este contador o efeito e invisivel: o registro sai valido e
            marcado OK, sem indicar que rodou sem enum/minimum/maximum.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    api_calls: int = 0
    corrections: int = 0
    schema_fallbacks: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )
    # True quando ao menos um record() veio de backend cujo input_tokens
    # EXCLUI os tokens de cache (Anthropic). Determina como total_tokens soma.
    _input_excludes_cache: bool = field(default=False, repr=False, compare=False)

    def record(
        self,
        input_tok: int,
        output_tok: int,
        is_correction: bool = False,
        cache_write_tok: int = 0,
        cache_read_tok: int = 0,
        input_excludes_cache: bool = False,
    ) -> None:
        """Registra tokens de uma chamada LLM.

        Os kwargs de cache tem default 0, de modo que chamadores que nao os
        informam continuam funcionando sem alteracao.

        Args:
            input_tok: Tokens de entrada desta chamada.
            output_tok: Tokens de saida desta chamada.
            is_correction: True se for uma chamada de correcao (fix/fix_async).
            cache_write_tok: Tokens gravados no cache nesta chamada.
            cache_read_tok: Tokens lidos do cache nesta chamada.
            input_excludes_cache: True quando `input_tok` NAO inclui os tokens
                de cache (backend Anthropic). False quando ja os inclui
                (OpenAI-compat). Ver docstring do modulo.
        """
        with self._lock:
            self.input_tokens += input_tok
            self.output_tokens += output_tok
            self.cache_write_tokens += cache_write_tok
            self.cache_read_tokens += cache_read_tok
            if input_excludes_cache:
                self._input_excludes_cache = True
            self.api_calls += 1
            if is_correction:
                self.corrections += 1

    def record_schema_fallback(self) -> None:
        """Registra que o caminho JSON foi abandonado para texto livre."""
        with self._lock:
            self.schema_fallbacks += 1

    @property
    def total_prompt_tokens(self) -> int:
        """Total de tokens do prompt, incluindo os servidos por cache.

        No backend Anthropic soma input + cache_write + cache_read (campos
        disjuntos). No OpenAI-compat `input_tokens` ja e o total, entao os
        campos de cache NAO sao somados — evita dupla contagem.
        """
        if self._input_excludes_cache:
            return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens
        return self.input_tokens

    @property
    def total_tokens(self) -> int:
        """Soma do prompt (incl. cache) + output."""
        return self.total_prompt_tokens + self.output_tokens

    def summary_line(self) -> str:
        """Linha formatada para exibicao no terminal (formato verbose).

        O segmento de cache so aparece quando ha atividade de cache, mantendo
        a linha enxuta em provedores que nao o suportam.

        Returns:
            String no formato:
            "tokens: in X,XXX | out X,XXX | total X,XXX | calls N"
            Com "| cache w X,XXX/r X,XXX" quando houve atividade de cache e
            "| corrections N" quando corrections > 0.
        """
        parts = [
            f"tokens: in {self.input_tokens:,}",
            f"out {self.output_tokens:,}",
            f"total {self.total_tokens:,}",
            f"calls {self.api_calls}",
        ]
        if self.cache_write_tokens or self.cache_read_tokens:
            parts.append(
                f"cache w {self.cache_write_tokens:,}/r {self.cache_read_tokens:,}"
            )
        if self.corrections:
            parts.append(f"corrections {self.corrections}")
        if self.schema_fallbacks:
            parts.append(f"schema-fallbacks {self.schema_fallbacks}")
        return " | ".join(parts)

    def reset(self) -> None:
        """Reinicia todos os contadores."""
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.cache_write_tokens = 0
            self.cache_read_tokens = 0
            self.api_calls = 0
            self.corrections = 0
            self.schema_fallbacks = 0
            self._input_excludes_cache = False
