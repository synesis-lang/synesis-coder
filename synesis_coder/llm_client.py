"""Cliente LLM para o synesis-coder.

Suporta dois backends:
- "anthropic" (padrão): API Anthropic (claude-opus-4-6)
- "openai": APIs OpenAI-compatíveis — Ollama local, RunPod, Together AI, etc.

Seleção de backend via env var SYNESIS_CODER_BACKEND (padrão: "anthropic").

Formato interno de mensagens (agnóstico ao provedor):
    [
        {"role": "system", "content": str, "cache": bool},
        {"role": "user",   "content": str, "cache": bool},
    ]

LLMClient traduz para o formato do backend selecionado internamente.
Suporta chamadas síncronas (call) e assíncronas (call_async) com
rate limiting compartilhado via asyncio.Semaphore + deques.

Rate limiting é ativado apenas no backend Anthropic (APIs locais não têm cotas externas).
Prompt caching (cache_control) é aplicado apenas no backend Anthropic;
o campo "cache" é silenciosamente ignorado no backend OpenAI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, List, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from synesis_coder.debug_log import DebugRecorder

# Carrega .env (variáveis de ambiente têm precedência)
load_dotenv()

_log = logging.getLogger(__name__)

_DEFAULT_BACKEND = "anthropic"
_DEFAULT_API_URL = "http://localhost:11434"
_DEFAULT_MAX_TOKENS = 4096


def _get_backend() -> str:
    return os.environ.get("SYNESIS_CODER_BACKEND", _DEFAULT_BACKEND).lower()


def _get_api_url() -> str:
    return os.environ.get("SYNESIS_CODER_API_URL", _DEFAULT_API_URL)


def _get_anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY não encontrada. "
            "Crie um arquivo .env baseado em .env.example e defina sua chave."
        )
    return key


def _get_api_key() -> str:
    """Retorna API key para backend OpenAI-compatível (opcional — Ollama não exige)."""
    return os.environ.get("SYNESIS_CODER_API_KEY", "no-key-required")


def _get_model() -> str:
    return os.environ.get("SYNESIS_CODER_MODEL", "claude-opus-4-6")


def _get_max_retries() -> int:
    return int(os.environ.get("SYNESIS_CODER_MAX_RETRIES", "3"))


# Modelos compatíveis com extended thinking (Anthropic Claude 4.x)
_THINKING_CAPABLE_MODELS = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5-20250929",
    "claude-3-7-sonnet-latest",
    "claude-3-7-sonnet-20250219",
})

# Modelos que deprecaram o parâmetro `temperature` — API retorna 400 se enviado
_TEMPERATURE_DEPRECATED_MODELS = frozenset({
    "claude-opus-4-7",
})


def _get_env_temperature() -> Optional[float]:
    """Lê SYNESIS_CODER_TEMPERATURE do ambiente. Retorna None se não definida."""
    v = os.environ.get("SYNESIS_CODER_TEMPERATURE")
    if v and v.strip():
        return float(v)
    return None


def _get_max_tokens_override() -> Optional[int]:
    """Lê SYNESIS_CODER_MAX_TOKENS do ambiente. Retorna None se não definida."""
    v = os.environ.get("SYNESIS_CODER_MAX_TOKENS")
    if v and v.strip():
        return int(v)
    return None


def _get_thinking_budget() -> int:
    """Lê SYNESIS_CODER_THINKING_BUDGET do ambiente. Retorna 0 se não definida."""
    return int(os.environ.get("SYNESIS_CODER_THINKING_BUDGET", "0"))


def _parse_json_response(raw: str) -> Optional[dict]:
    """Parseia a resposta JSON do caminho Opção 3, tolerando fences de markdown.

    Modelos às vezes envolvem o JSON em ```json ... ``` mesmo com response_format.
    Retorna None quando o conteúdo não é um objeto JSON válido — sinalizando
    fallback para texto livre.
    """
    import json
    import re

    text = (raw or "").strip()
    if not text:
        return None

    fenced = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    return parsed if isinstance(parsed, dict) else None


def _model_supports_thinking(model: str) -> bool:
    """Verifica se o modelo suporta extended thinking (Anthropic Claude 4.x)."""
    base = model.split(":")[0].lower()
    return any(base.startswith(m) for m in _THINKING_CAPABLE_MODELS)


def _model_deprecates_temperature(model: str) -> bool:
    """Verifica se o modelo rejeita o parâmetro temperature (ex: claude-opus-4-7)."""
    base = model.split(":")[0].lower()
    return any(base.startswith(m) for m in _TEMPERATURE_DEPRECATED_MODELS)


def _estimate_max_tokens(messages: list, model_cap: int) -> int:
    """Estima max_tokens ideal como min(teto_do_modelo, estimativa_por_chunk).

    Heurística: output ≈ len(chars_de_input) / 4 × 1.2.
    Piso: _DEFAULT_MAX_TOKENS para garantir espaço mínimo de resposta.
    Teto: model_cap (0 = desconhecido → usa só o piso).
    """
    total_chars = sum(len(m.get("content", "")) for m in messages)
    estimate = int(total_chars / 4 * 1.2)
    estimate = max(estimate, _DEFAULT_MAX_TOKENS)
    if model_cap > 0:
        return min(estimate, model_cap)
    return estimate


def _wait_honoring_retry_after(retry_state) -> float:
    """Retorna o tempo de espera respeitando o header Retry-After quando presente.

    Funciona para Anthropic (RateLimitError) e backends OpenAI-compat
    (APIStatusError 429): ambos expõem .response.headers['retry-after'].
    Fallback: backoff exponencial idêntico ao comportamento anterior.
    """
    from tenacity import wait_exponential
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except (ValueError, TypeError):
                pass
    return wait_exponential(multiplier=2, min=4, max=60)(retry_state)


class LLMClient:
    """Cliente síncrono para chamadas LLM com suporte a backends Anthropic e OpenAI-compatíveis.

    Gerencia:
    - Seleção de backend via SYNESIS_CODER_BACKEND ("anthropic" | "openai")
    - Rate limiting por RPM e TPM (janela deslizante de 60s) — apenas backend Anthropic
    - Sleep proativo antes de estourar cotas (margem de 15%) — apenas backend Anthropic
    - Retry com tenacity em erros de API (exceções por backend)
    """

    # Limites padrão conservadores para Tier 1 da Anthropic
    _DEFAULT_MAX_RPM = 50
    _DEFAULT_MAX_INPUT_TPM = 40_000
    _DEFAULT_MAX_OUTPUT_TPM = 8_000
    _SAFETY_MARGIN = 0.85  # usar até 85% da cota antes de pausar

    def __init__(
        self,
        model: Optional[str] = None,
        backend: Optional[str] = None,
        max_rpm: Optional[int] = None,
        max_input_tpm: Optional[int] = None,
        max_output_tpm: Optional[int] = None,
        recorder: Optional["DebugRecorder"] = None,
    ) -> None:
        """Inicializa o cliente.

        Args:
            model: ID do modelo (padrão: env SYNESIS_CODER_MODEL ou claude-opus-4-6).
            backend: Backend LLM ("anthropic" | "openai"). Padrão: env SYNESIS_CODER_BACKEND.
            max_rpm: Limite de requisições por minuto (apenas Anthropic).
            max_input_tpm: Limite de tokens de input por minuto (apenas Anthropic).
            max_output_tpm: Limite de tokens de output por minuto (apenas Anthropic).
            recorder: DebugRecorder opcional (flag --debug). None = sem overhead.
        """
        self.recorder = recorder
        self.model = model or _get_model()
        self.backend = (backend or _get_backend()).lower()

        if self.backend == "openai":
            import openai

            self._client = openai.OpenAI(
                base_url=f"{_get_api_url()}/v1",
                api_key=_get_api_key(),
            )
            self._rate_limit_enabled = False
            self._retryable_errors: tuple = (
                openai.APIStatusError,
                openai.APIConnectionError,
            )
        else:
            import anthropic

            self._client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
            self._rate_limit_enabled = True
            self._retryable_errors = (
                anthropic.RateLimitError,
                anthropic.APIStatusError,
            )

        self._max_rpm = max_rpm or int(
            os.environ.get("SYNESIS_CODER_MAX_RPM", self._DEFAULT_MAX_RPM)
        )
        self._max_input_tpm = max_input_tpm or int(
            os.environ.get("SYNESIS_CODER_MAX_INPUT_TPM", self._DEFAULT_MAX_INPUT_TPM)
        )
        self._max_output_tpm = max_output_tpm or int(
            os.environ.get("SYNESIS_CODER_MAX_OUTPUT_TPM", self._DEFAULT_MAX_OUTPUT_TPM)
        )

        # Janelas deslizantes de 60s: (timestamp, tokens)
        self._request_times: deque = deque()
        self._input_tokens: deque = deque()   # (timestamp, n_tokens)
        self._output_tokens: deque = deque()  # (timestamp, n_tokens)

        # Acumulador de tokens da sessao (exposto publicamente para os modos)
        from synesis_coder.token_usage import TokenUsage
        self.usage = TokenUsage()

        # Flag thread-local: marcado em fix()/fix_async() para que _record_usage()
        # saiba que a proxima chamada e uma correcao. threading.local() garante
        # que chamadas fix_async() concorrentes nao disputem o mesmo flag.
        self._correction_local = threading.local()

        # Cache para teto de tokens do modelo: None = não consultado ainda,
        # 0 = consultado mas indisponível, >0 = teto real em tokens.
        self._model_output_cap: Optional[int] = None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def call(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        thinking: bool = True,
        thinking_budget: Optional[int] = None,
    ) -> str:
        """Envia mensagens ao LLM e retorna o texto de resposta.

        Args:
            messages: Lista no formato interno:
                [{"role": "system"|"user"|"assistant", "content": str, "cache": bool}]
            temperature: Temperatura da geração (0 = determinístico).
                Sobrescrita por SYNESIS_CODER_TEMPERATURE quando thinking=True.
            max_tokens: Máximo de tokens de output.
                Sobrescrita por SYNESIS_CODER_MAX_TOKENS quando thinking=True.
            thinking: Se False, desativa extended thinking (Anthropic) e reasoning
                extra (Qwen3). Use False em suggest/finetune.
            thinking_budget: Tokens de raciocínio interno (Anthropic, thinking=True).
                None = lê SYNESIS_CODER_THINKING_BUDGET do ambiente.

        Returns:
            Texto da resposta do assistente.
        """
        self._wait_if_rate_limited()
        return self._call_sync_inner(messages, temperature, max_tokens, thinking, thinking_budget)

    def fix(
        self,
        previous_output: str,
        errors: str,
        temperature: float = 0.2,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        """Solicita correção de output inválido com base nos erros do compilador.

        Args:
            previous_output: Output Synesis inválido gerado anteriormente.
            errors: Diagnósticos do compilador (result.get_diagnostics()).
            temperature: Temperatura para esta tentativa de correção.
            max_tokens: Máximo de tokens de output.

        Returns:
            Novo output corrigido.
        """
        fix_messages = [
            {
                "role": "user",
                "content": (
                    "The generated output contains Synesis syntax errors. "
                    "Fix it so it becomes valid.\n\n"
                    "OUTPUT WITH ERRORS:\n"
                    f"```\n{previous_output}\n```\n\n"
                    "COMPILER ERRORS:\n"
                    f"{errors}\n\n"
                    "Output only the corrected block(s), no explanations."
                ),
                "cache": False,
            }
        ]
        self._correction_local.is_correction = True
        return self.call(fix_messages, temperature=temperature, max_tokens=max_tokens, thinking=False)

    # ------------------------------------------------------------------
    # API assíncrona (para modos de lote: abstract, document, ontology)
    # ------------------------------------------------------------------

    async def call_async(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        thinking: bool = True,
        thinking_budget: Optional[int] = None,
        context: Optional[tuple] = None,
    ) -> str:
        """Versão assíncrona de call() — delega ao thread pool com rate limiting.

        Usa asyncio.to_thread para não bloquear o event loop durante a chamada
        HTTP síncrona. O rate limiting é proativo (sleep antes da chamada).

        Args:
            messages: Lista no formato interno.
            temperature: Temperatura da geração.
            max_tokens: Máximo de tokens de output.
            thinking: Se False, desativa extended thinking e reasoning extra (Qwen3).
            thinking_budget: Tokens de raciocínio. None = lê env SYNESIS_CODER_THINKING_BUDGET.
            context: Contexto do recorder (ex.: ("chunk", 2, 7)); setado dentro
                da thread worker pois threading.local() é por-thread.

        Returns:
            Texto da resposta do assistente.
        """
        await self._async_wait_if_rate_limited()

        if self.recorder is None:
            return await asyncio.to_thread(
                self._call_sync_inner, messages, temperature, max_tokens,
                thinking, thinking_budget,
            )

        def _call_in_thread() -> str:
            self.recorder.set_context(context)
            return self._call_sync_inner(
                messages, temperature, max_tokens, thinking, thinking_budget
            )

        return await asyncio.to_thread(_call_in_thread)

    async def fix_async(
        self,
        previous_output: str,
        errors: str,
        temperature: float = 0.2,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        context: Optional[tuple] = None,
    ) -> str:
        """Versão assíncrona de fix() — solicita correção de output inválido."""
        fix_messages = [
            {
                "role": "user",
                "content": (
                    "The generated output contains Synesis syntax errors. "
                    "Fix it so it becomes valid.\n\n"
                    "OUTPUT WITH ERRORS:\n"
                    f"```\n{previous_output}\n```\n\n"
                    "COMPILER ERRORS:\n"
                    f"{errors}\n\n"
                    "Output only the corrected block(s), no explanations."
                ),
                "cache": False,
            }
        ]
        # O flag de correcao deve ser setado dentro da thread worker que executa
        # _call_sync_inner, pois threading.local() e por-thread. O wrapper abaixo
        # garante isso sem alterar a assinatura de _call_sync_inner.
        await self._async_wait_if_rate_limited()

        def _fix_in_thread() -> str:
            self._correction_local.is_correction = True
            if self.recorder is not None:
                self.recorder.set_context(context)
            return self._call_sync_inner(fix_messages, temperature, max_tokens, thinking=False)

        return await asyncio.to_thread(_fix_in_thread)

    # ------------------------------------------------------------------
    # Caminho JSON (Opção 3): LLM devolve valores; Python monta o bloco
    # ------------------------------------------------------------------

    def supports_json_schema(self) -> bool:
        """True se o backend ativo aceita `response_format: json_schema`.

        Atualmente só o backend OpenAI-compatível (Gemini via API OpenAI, etc.)
        suporta. O backend Anthropic nativo usa outro mecanismo — tratado como
        não-suportado para que o chamador caia no caminho de texto livre.
        """
        return self.backend == "openai"

    def call_json(
        self,
        messages: List[dict],
        schema: dict,
        temperature: Optional[float] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> Optional[dict]:
        """Chama o LLM pedindo JSON conforme `schema` e retorna o dict parseado.

        Retorna None quando o backend não suporta json_schema, quando a API
        recusa o `response_format` (erro 400) ou quando a resposta não é JSON
        válido — sinalizando ao chamador para cair no caminho de texto livre.

        Args:
            messages: Mensagens no formato interno.
            schema: JSON Schema (de schema_builder) descrevendo os valores.
            temperature: Temperatura da geração.
            max_tokens: Máximo de tokens de output.

        Returns:
            dict parseado, ou None para acionar fallback.
        """
        if not self.supports_json_schema():
            return None

        self._wait_if_rate_limited()
        try:
            raw = self._call_sync_inner(
                messages, temperature, max_tokens, thinking=False, schema=schema
            )
        except Exception as exc:
            _log.warning(
                "Caminho JSON falhou na chamada ao backend (%s) — "
                "caindo para texto livre.", exc,
            )
            return None

        return _parse_json_response(raw)

    async def call_json_async(
        self,
        messages: List[dict],
        schema: dict,
        temperature: Optional[float] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        context: Optional[tuple] = None,
    ) -> Optional[dict]:
        """Versão assíncrona de call_json() para modos de lote."""
        if not self.supports_json_schema():
            return None

        await self._async_wait_if_rate_limited()

        def _call_in_thread() -> str:
            if self.recorder is not None:
                self.recorder.set_context(context)
            return self._call_sync_inner(
                messages, temperature, max_tokens, thinking=False, schema=schema
            )

        try:
            raw = await asyncio.to_thread(_call_in_thread)
        except Exception as exc:
            _log.warning(
                "Caminho JSON (async) falhou na chamada ao backend (%s) — "
                "caindo para texto livre.", exc,
            )
            return None

        return _parse_json_response(raw)

    def _discover_model_output_cap(self) -> int:
        """Retorna o teto de tokens de output do modelo via API (lazy + cached).

        Retorna 0 se o teto não puder ser determinado (provedor sem suporte,
        erro de rede, modelo não encontrado). O resultado é cacheado em
        self._model_output_cap para evitar chamadas repetidas.

        Backends suportados:
        - Anthropic: GET /v1/models/{id} → campo max_tokens
        - OpenAI-compat (Google AI Studio gemini-*): apenas modelos Gemini têm
          endpoint /v1/models — por ora retorna 0 (fallback para estimativa).
        """
        if self._model_output_cap is not None:
            return self._model_output_cap

        cap = 0
        try:
            if self.backend == "anthropic":
                model_info = self._client.models.retrieve(self.model)
                cap = getattr(model_info, "max_tokens", 0) or 0
        except Exception as exc:
            _log.debug("Não foi possível obter teto do modelo '%s': %s", self.model, exc)

        self._model_output_cap = cap
        if cap > 0:
            _log.debug("Teto de output do modelo '%s': %d tokens", self.model, cap)
        return cap

    def _call_sync_inner(
        self,
        messages: List[dict],
        temperature: Optional[float],
        max_tokens: int,
        thinking: bool = True,
        thinking_budget: Optional[int] = None,
        schema: Optional[dict] = None,
    ) -> str:
        """Lógica interna de chamada síncrona com retry (usada por call e call_async).

        Quando `schema` é fornecido e o backend é OpenAI-compatível, envia
        `response_format` com `json_schema` strict — o modelo devolve JSON de
        valores (Opção 3). O parsing/validação fica a cargo do chamador (call_json).
        """
        from tenacity import retry, retry_if_exception_type, stop_after_attempt

        # Env override de temperatura — aplicado a todos os backends e modos analíticos.
        # Tem precedência sobre o argumento; None significa "deixar o modelo decidir".
        env_temp = _get_env_temperature()
        if env_temp is not None:
            temperature = env_temp

        # Precedência de max_tokens:
        #   1. SYNESIS_CODER_MAX_TOKENS (env) — vence tudo
        #   2. min(teto_via_API, estimativa_por_chunk) — dinâmico, padrão quando
        #      o chamador não passou valor explícito (max_tokens == _DEFAULT_MAX_TOKENS)
        #   3. Valor passado pelo chamador (ex.: suggest_mode com 512)
        env_max = _get_max_tokens_override()
        if env_max is not None:
            max_tokens = env_max
        elif max_tokens == _DEFAULT_MAX_TOKENS:
            model_cap = self._discover_model_output_cap()
            dynamic = _estimate_max_tokens(messages, model_cap)
            if dynamic != max_tokens:
                _log.debug(
                    "max_tokens dinâmico: %d (cap=%d)", dynamic, model_cap
                )
            max_tokens = dynamic

        retryable = self._retryable_errors

        if self.backend == "openai":
            _, api_messages = self._translate_messages(messages)

            # Qwen3 e Kimi (Moonshot) suportam desativar reasoning via extra_body={"think": false}
            extra: dict = {}
            _model_lower = self.model.lower()
            if not thinking and any(m in _model_lower for m in ("qwen3", "kimi")):
                extra["think"] = False

            @retry(
                retry=retry_if_exception_type(retryable),
                stop=stop_after_attempt(_get_max_retries()),
                wait=_wait_honoring_retry_after,
                reraise=True,
            )
            def _call_with_retry() -> str:
                create_kwargs: dict = {
                    "model": self.model,
                    "messages": api_messages,
                    "max_tokens": max_tokens,
                }
                # Só envia temperature se explicitamente configurado — evita 400
                # em modelos com temperatura fixa (ex: kimi-k2.6 exige apenas 1).
                if temperature is not None:
                    create_kwargs["temperature"] = temperature
                if extra:
                    create_kwargs["extra_body"] = extra
                if schema is not None:
                    create_kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "synesis_values",
                            "schema": schema,
                            "strict": True,
                        },
                    }
                response = self._client.chat.completions.create(**create_kwargs)
                if response.usage:
                    is_correction = getattr(self._correction_local, "is_correction", False)
                    self._correction_local.is_correction = False
                    self.usage.record(
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        is_correction=is_correction,
                    )
                choice = response.choices[0]
                finish = getattr(choice, "finish_reason", None)
                if finish == "length":
                    _log.warning(
                        "LLM truncou a resposta (finish_reason=length, max_tokens=%d). "
                        "Considere aumentar SYNESIS_CODER_MAX_TOKENS.",
                        max_tokens,
                    )
                msg = choice.message
                content = msg.content or ""
                if not content.strip():
                    # Modelos de raciocínio (kimi-k2.6, DeepSeek-R1) às vezes
                    # deixam content vazio e colocam o output em reasoning_content.
                    content = getattr(msg, "reasoning_content", None) or ""
                return content

        else:
            system_blocks, api_messages = self._translate_messages(messages)

            # Resolve budget: argumento explícito > variável de ambiente
            budget = (
                (thinking_budget if thinking_budget is not None else _get_thinking_budget())
                if thinking else 0
            )
            use_thinking = budget > 0

            if use_thinking and not _model_supports_thinking(self.model):
                _log.warning(
                    "SYNESIS_CODER_THINKING_BUDGET=%d ignorado: modelo '%s' não suporta "
                    "extended thinking. Modelos compatíveis: claude-opus-4-7, "
                    "claude-opus-4-6, claude-sonnet-4-6. "
                    "Altere SYNESIS_CODER_MODEL para ativar.",
                    budget, self.model,
                )
                use_thinking = False

            @retry(
                retry=retry_if_exception_type(retryable),
                stop=stop_after_attempt(_get_max_retries()),
                wait=_wait_honoring_retry_after,
                reraise=True,
            )
            def _call_with_retry() -> str:
                kwargs: dict = {
                    "model": self.model,
                    "max_tokens": max(max_tokens, budget + 4096) if use_thinking else max_tokens,
                    "messages": api_messages,
                }
                if use_thinking:
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                    kwargs["temperature"] = 1.0
                elif not _model_deprecates_temperature(self.model):
                    # None → determinístico (0.0); valor explícito/env → respeitar
                    kwargs["temperature"] = temperature if temperature is not None else 0.0
                # else: modelo deprecou temperature — omitir (ex: claude-opus-4-7)
                if system_blocks:
                    kwargs["system"] = system_blocks

                response = self._client.messages.create(**kwargs)
                self._record_usage(response.usage)

                if response.stop_reason == "max_tokens":
                    _log.warning(
                        "LLM truncou a resposta (stop_reason=max_tokens, max_tokens=%d). "
                        "Considere aumentar SYNESIS_CODER_MAX_TOKENS.",
                        kwargs["max_tokens"],
                    )

                # Itera blocos — com thinking ativo, content[0] é ThinkingBlock
                for block in response.content:
                    if block.type == "text":
                        return block.text
                raise RuntimeError(
                    f"Resposta Anthropic sem bloco text — "
                    f"tipos recebidos: {[b.type for b in response.content]}"
                )

        if self.recorder is None:
            return _call_with_retry()

        # Caminho instrumentado (--debug): mede latência e emite evento bruto.
        tokens_before_in = self.usage.input_tokens
        tokens_before_out = self.usage.output_tokens
        is_fix = getattr(self._correction_local, "is_correction", False)
        t0 = time.monotonic()
        result = _call_with_retry()
        latency_ms = (time.monotonic() - t0) * 1000.0

        system_text = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        user_text = "\n\n".join(
            m["content"] for m in messages if m.get("role") != "system"
        )
        self.recorder.record_llm_call(
            phase="fix" if is_fix else self._recorder_phase(),
            system=system_text,
            user=user_text,
            raw=result,
            latency_ms=latency_ms,
            input_tokens=self.usage.input_tokens - tokens_before_in,
            output_tokens=self.usage.output_tokens - tokens_before_out,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result

    def _recorder_phase(self) -> str:
        """Fase para o recorder a partir do contexto da thread atual.

        A fase é o primeiro elemento do contexto (ex.: "source", "chunk",
        "entry") — o renderer casa gen_call.phase com recorder.unit_type. O
        contexto é setado pelo chamador via recorder.set_context()/context=.
        Fallback "chunk" preserva o comportamento quando não há contexto.
        """
        ctx = getattr(self.recorder._tls, "context", None) if self.recorder else None
        if ctx:
            return ctx[0]
        return "chunk"

    async def _async_wait_if_rate_limited(self) -> None:
        """Versão assíncrona de _wait_if_rate_limited — usa asyncio.sleep."""
        if not self._rate_limit_enabled:
            return

        now = time.monotonic()
        window = 60.0

        for dq in (self._request_times, self._input_tokens, self._output_tokens):
            while dq and now - dq[0][0] > window:
                dq.popleft()

        # RPM
        if len(self._request_times) >= int(self._max_rpm * self._SAFETY_MARGIN):
            oldest = self._request_times[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Input TPM
        input_used = sum(t for _, t in self._input_tokens)
        if input_used >= int(self._max_input_tpm * self._SAFETY_MARGIN):
            oldest = self._input_tokens[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Output TPM
        output_used = sum(t for _, t in self._output_tokens)
        if output_used >= int(self._max_output_tpm * self._SAFETY_MARGIN):
            oldest = self._output_tokens[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _wait_if_rate_limited(self) -> None:
        """Pausa proativamente se próxima chamada ultrapassaria as cotas."""
        if not self._rate_limit_enabled:
            return

        now = time.monotonic()
        window = 60.0

        # Limpar entradas antigas (> 60s)
        for dq in (self._request_times, self._input_tokens, self._output_tokens):
            while dq and now - dq[0][0] > window:
                dq.popleft()

        # Verificar RPM
        if len(self._request_times) >= int(self._max_rpm * self._SAFETY_MARGIN):
            oldest = self._request_times[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Verificar TPM de input
        input_used = sum(t for _, t in self._input_tokens)
        if input_used >= int(self._max_input_tpm * self._SAFETY_MARGIN):
            oldest = self._input_tokens[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Verificar TPM de output
        output_used = sum(t for _, t in self._output_tokens)
        if output_used >= int(self._max_output_tpm * self._SAFETY_MARGIN):
            oldest = self._output_tokens[0][0]
            sleep_time = window - (now - oldest) + 0.1
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _record_usage(self, usage) -> None:
        """Registra uso de tokens após chamada bem-sucedida (apenas Anthropic)."""
        now = time.monotonic()
        self._request_times.append((now, 1))
        self._input_tokens.append((now, usage.input_tokens))
        self._output_tokens.append((now, usage.output_tokens))
        # Acumulador de sessao — le e reseta o flag de correcao da thread corrente
        is_correction = getattr(self._correction_local, "is_correction", False)
        self._correction_local.is_correction = False
        self.usage.record(usage.input_tokens, usage.output_tokens, is_correction=is_correction)

    # ------------------------------------------------------------------
    # Tradução de formato interno → formato do backend
    # ------------------------------------------------------------------

    def _translate_messages(
        self, messages: List[dict]
    ) -> tuple[list, list]:
        """Converte formato interno para o formato do backend selecionado.

        Returns:
            (system_blocks, api_messages)
            - Anthropic: system_blocks com cache_control; api_messages sem system
            - OpenAI: system_blocks vazio; api_messages inclui role=system
        """
        if self.backend == "openai":
            return self._translate_messages_openai(messages)
        return self._translate_messages_anthropic(messages)

    def _translate_messages_anthropic(
        self, messages: List[dict]
    ) -> tuple[list, list]:
        """Converte formato interno para system_blocks + messages da API Anthropic."""
        system_blocks = []
        api_messages = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            use_cache = msg.get("cache", False)

            if role == "system":
                block: dict = {"type": "text", "text": content}
                if use_cache:
                    block["cache_control"] = {"type": "ephemeral"}
                system_blocks.append(block)
            else:
                # user ou assistant
                if use_cache:
                    content_block: dict = {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                    api_messages.append({"role": role, "content": [content_block]})
                else:
                    api_messages.append({"role": role, "content": content})

        return system_blocks, api_messages

    def _translate_messages_openai(
        self, messages: List[dict]
    ) -> tuple[list, list]:
        """Converte formato interno para messages da API OpenAI-compatível.

        O campo "cache" é ignorado silenciosamente (OpenAI-compatível não suporta
        prompt caching no mesmo formato da Anthropic).

        Returns:
            ([], api_messages) — system_blocks sempre vazio; system incluso em api_messages
        """
        api_messages = []
        for msg in messages:
            api_messages.append({"role": msg["role"], "content": msg["content"]})
        return [], api_messages
