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


def get_critique_connection() -> dict:
    """Resolve a conexão (backend/api_url/api_key) da fase de CRÍTICA.

    A "conexão de crítica" é usada pela fase `critique` e pelo crítico do modo
    `refine`. Permite avaliar as anotações num provedor distinto do gerador
    (independência epistêmica real), sem afetar as demais fases.

    Precedência por eixo: var SYNESIS_CODER_CRITIQUE_* → conexão global.
    Se NENHUMA var CRITIQUE_* de conexão for definida, retorna dict cujos valores
    são None → o LLMClient cai no fallback de ambiente do backend resolvido,
    reproduzindo o comportamento atual (retrocompatível).

    O `api_key` fica None quando não há CRITIQUE_API_KEY explícita: o __init__
    então resolve a chave certa para o backend da crítica (ANTHROPIC_API_KEY se
    anthropic, SYNESIS_CODER_API_KEY se openai) — que pode diferir do backend
    primário.

    Returns:
        Dict com chaves "backend", "api_url", "api_key" pronto para
        LLMClient(model=..., **conn). Valores None acionam o fallback do __init__.
    """
    return {
        "backend": os.environ.get("SYNESIS_CODER_CRITIQUE_BACKEND") or None,
        "api_url": os.environ.get("SYNESIS_CODER_CRITIQUE_API_URL") or None,
        "api_key": os.environ.get("SYNESIS_CODER_CRITIQUE_API_KEY") or None,
    }


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


# Keywords de JSON Schema que o constrained decoding do structured outputs da
# Anthropic NÃO aceita (retorna HTTP 400). Removidos do schema enviado ao wire;
# a garantia real desses limites permanece no validate_and_fix (compilador).
# Ref: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
_ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
})

# Cache de módulo: o SDK anthropic instalado expõe `output_config`? None = não
# consultado ainda. Determina se o backend anthropic pode usar o caminho JSON.
_anthropic_output_config_support: Optional[bool] = None


def _anthropic_sdk_supports_output_config() -> bool:
    """True se o SDK anthropic instalado aceita `output_config` (structured outputs).

    `output_config` chegou no SDK anthropic 0.77.0. Em SDKs anteriores, o
    backend anthropic deve cair no caminho de texto livre (comportamento
    pré-structured-outputs) em vez de enviar um kwarg desconhecido à API.

    O resultado é cacheado a nível de módulo (a assinatura não muda em runtime).
    """
    global _anthropic_output_config_support
    if _anthropic_output_config_support is not None:
        return _anthropic_output_config_support

    supported = False
    try:
        import inspect

        from anthropic.resources.messages import Messages

        params = inspect.signature(Messages.create).parameters
        supported = "output_config" in params
    except Exception as exc:
        _log.debug("Não foi possível inspecionar o SDK anthropic: %s", exc)
        supported = False

    _anthropic_output_config_support = supported
    return supported


def _sanitize_schema_for_anthropic(schema: dict) -> dict:
    """Remove recursivamente keywords de JSON Schema não suportados pela Anthropic.

    Retorna uma cópia profunda saneada — o schema original (usado pelo backend
    OpenAI, que aceita esses keywords) é preservado intacto. A remoção de
    `minimum`/`maximum` etc. não enfraquece a validação: o `validate_and_fix`
    roda sempre depois e o compilador Synesis reprova valores fora do range.
    """
    if isinstance(schema, dict):
        return {
            k: _sanitize_schema_for_anthropic(v)
            for k, v in schema.items()
            if k not in _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [_sanitize_schema_for_anthropic(v) for v in schema]
    return schema


class TokenBudgetExhausted(RuntimeError):
    """A resposta acabou sem bloco de texto porque o orçamento de tokens esgotou.

    Modelos de raciocínio (gpt-5.6-luna, DeepSeek-R1, kimi) podem pensar por
    conta própria mesmo com `thinking=False` no payload — o coder não controla
    isso. Quando o raciocínio consome todo o `max_tokens`, a resposta chega com
    `stop_reason="max_tokens"` e apenas blocos de thinking, sem `text`.

    É uma condição OPERACIONAL (orçamento insuficiente), não uma limitação do
    backend — daí a classe própria: permite ao chamador distinguir "aumente
    max_tokens" de "este backend não suporta schema", que exigem ações opostas.
    """

    def __init__(self, block_types: list[str], max_tokens: int) -> None:
        self.block_types = block_types
        self.max_tokens = max_tokens
        super().__init__(
            f"resposta sem bloco text (tipos: {block_types}) e truncada por "
            f"max_tokens={max_tokens}"
        )


def _provider_requires_explicit_cache(model: str) -> bool:
    """True se o provedor por trás deste ID de modelo exige `cache_control`.

    No OpenRouter o ID carrega o provedor como prefixo (`anthropic/claude-*`,
    `qwen/*`). Anthropic e Qwen exigem breakpoints explícitos; os demais
    provedores OpenAI-compatíveis fazem caching automático por prefixo, e
    marcar blocos ali seria inócuo (na melhor hipótese) ou rejeitado.

    Heurística por prefixo de ID — daí ser conservadora: na dúvida, não marca.
    """
    base = model.split(":")[0].lower()
    return base.startswith(("anthropic/", "qwen/"))


# Teto absoluto para a re-tentativa do caminho JSON após esgotamento de
# orçamento. Acima disso, insistir custa mais do que a garantia do schema vale.
_RETRY_MAX_TOKENS_CAP = 64000

# Temperatura da re-tentativa quando a resposta não é JSON parseável.
# A geração roda em temperature=0.0; repetir com o MESMO valor tende a
# reproduzir a mesma saída malformada. Um incremento pequeno basta para sair do
# modo de falha sem soltar a extração (mesma lógica do escalonamento em
# validator.CORRECTION_TEMPERATURES).
_MALFORMED_JSON_RETRY_TEMPERATURE = 0.2


def _retry_max_tokens(previous: int) -> Optional[int]:
    """Calcula o `max_tokens` da re-tentativa (dobra, limitado ao teto).

    Retorna None quando já se está no teto — nesse caso o chamador desiste do
    caminho JSON em vez de repetir uma chamada que falharia igual.
    """
    if previous >= _RETRY_MAX_TOKENS_CAP:
        return None
    return min(previous * 2, _RETRY_MAX_TOKENS_CAP)


def _int_attr(obj, name: str) -> int:
    """Lê um atributo numérico opcional de um objeto de usage, com fallback 0.

    Defensivo por três motivos:
    - nem todo provedor OpenAI-compatível preenche `prompt_tokens_details`;
    - SDKs antigos podem não tipar os campos de cache;
    - em testes o objeto de usage costuma ser um MagicMock, cujo getattr
      devolve outro MagicMock (não um int) — daí a checagem de tipo em vez
      de confiar no default do getattr.
    """
    if obj is None:
        return 0
    value = getattr(obj, name, 0)
    return value if isinstance(value, int) else 0


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
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_rpm: Optional[int] = None,
        max_input_tpm: Optional[int] = None,
        max_output_tpm: Optional[int] = None,
        recorder: Optional["DebugRecorder"] = None,
    ) -> None:
        """Inicializa o cliente.

        Args:
            model: ID do modelo (padrão: env SYNESIS_CODER_MODEL ou claude-opus-4-6).
            backend: Backend LLM ("anthropic" | "openai"). Padrão: env SYNESIS_CODER_BACKEND.
            api_url: Base URL do backend openai-compat. None = env SYNESIS_CODER_API_URL.
                Ignorado no backend anthropic. Permite uma conexão explícita
                (ex.: 2ª API para a fase de crítica), independente da global.
            api_key: Chave de API. None = env (ANTHROPIC_API_KEY no backend anthropic,
                SYNESIS_CODER_API_KEY no backend openai). Permite conexão explícita.
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
                base_url=f"{api_url or _get_api_url()}/v1",
                api_key=api_key or _get_api_key(),
            )
            self._rate_limit_enabled = False
            self._retryable_errors: tuple = (
                openai.APIStatusError,
                openai.APIConnectionError,
            )
        else:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=api_key or _get_anthropic_api_key()
            )
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

    @staticmethod
    def _build_fix_messages(
        previous_output: str, errors: str, system: Optional[str] = None
    ) -> List[dict]:
        """Monta as mensagens da chamada de correção.

        Quando `system` é fornecido, ele entra como PRIMEIRA mensagem, marcada
        para cache. Isso é essencial por dois motivos:

        1. Sem o system prompt, o modelo corrige sem as GUIDELINES do template
           (réguas de score, proibições de domínio, code_index) e degrada o
           output a cada iteração do loop de correção.
        2. Sendo byte-a-byte idêntico ao da geração, o prefixo casa com o cache
           já gravado — o reenvio custa ~0.1x (Anthropic) em vez de 1.0x.
        """
        messages: List[dict] = []
        if system:
            messages.append({"role": "system", "content": system, "cache": True})
        messages.append(
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
        )
        return messages

    def fix(
        self,
        previous_output: str,
        errors: str,
        temperature: float = 0.2,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        system: Optional[str] = None,
        schema: Optional[dict] = None,
    ) -> str:
        """Solicita correção de output inválido com base nos erros do compilador.

        Args:
            previous_output: Output Synesis inválido gerado anteriormente.
            errors: Diagnósticos do compilador (result.get_diagnostics()).
            temperature: Temperatura para esta tentativa de correção.
            max_tokens: Máximo de tokens de output.
            system: System prompt da geração (GUIDELINES do template). None
                preserva o comportamento antigo (correção sem contexto).
            schema: JSON Schema da geração. Quando fornecido e o backend
                suporta, a correção mantém as garantias estruturais em vez de
                voltar ao texto livre.

        Returns:
            Novo output corrigido.
        """
        fix_messages = self._build_fix_messages(previous_output, errors, system)
        self._correction_local.is_correction = True
        use_schema = schema if (schema is not None and self.supports_json_schema()) else None
        self._wait_if_rate_limited()
        return self._call_sync_inner(
            fix_messages, temperature, max_tokens, thinking=False, schema=use_schema,
        )

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
        system: Optional[str] = None,
        schema: Optional[dict] = None,
    ) -> str:
        """Versão assíncrona de fix() — solicita correção de output inválido.

        `system` e `schema` seguem a semântica de fix(): sem eles a correção
        roda cega (comportamento antigo); com eles preserva as GUIDELINES e as
        garantias estruturais da geração.
        """
        fix_messages = self._build_fix_messages(previous_output, errors, system)
        use_schema = schema if (schema is not None and self.supports_json_schema()) else None
        # O flag de correcao deve ser setado dentro da thread worker que executa
        # _call_sync_inner, pois threading.local() e por-thread. O wrapper abaixo
        # garante isso sem alterar a assinatura de _call_sync_inner.
        await self._async_wait_if_rate_limited()

        def _fix_in_thread() -> str:
            self._correction_local.is_correction = True
            if self.recorder is not None:
                self.recorder.set_context(context)
            return self._call_sync_inner(
                fix_messages, temperature, max_tokens, thinking=False, schema=use_schema,
            )

        return await asyncio.to_thread(_fix_in_thread)

    # ------------------------------------------------------------------
    # Caminho JSON (Opção 3): LLM devolve valores; Python monta o bloco
    # ------------------------------------------------------------------

    def supports_json_schema(self) -> bool:
        """True se o backend ativo aceita saída estruturada por JSON Schema.

        - OpenAI-compatível: via `response_format: json_schema` (sempre).
        - Anthropic nativo: via `output_config.format` (structured outputs),
          disponível apenas se o SDK instalado for >= 0.77. Em SDKs anteriores
          retorna False → o chamador cai no caminho de texto livre, preservando
          o comportamento pré-structured-outputs (degradação graciosa).
        """
        if self.backend == "openai":
            return True
        if self.backend == "anthropic":
            return _anthropic_sdk_supports_output_config()
        return False

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
        except TokenBudgetExhausted as exc:
            retry_tokens = _retry_max_tokens(exc.max_tokens)
            if retry_tokens is None:
                self._log_budget_exhausted(exc, retried=False)
                return None
            _log.warning(
                "Orçamento de tokens esgotado no raciocínio (tipos: %s, "
                "max_tokens=%d) — repetindo o caminho JSON com max_tokens=%d.",
                exc.block_types, exc.max_tokens, retry_tokens,
            )
            self._wait_if_rate_limited()
            try:
                raw = self._call_sync_inner(
                    messages, temperature, retry_tokens, thinking=False,
                    schema=schema, force_max_tokens=retry_tokens,
                )
            except Exception as exc2:
                self._log_budget_exhausted(exc2, retried=True)
                return None
        except Exception as exc:
            _log.warning(
                "Caminho JSON falhou na chamada ao backend (%s) — "
                "caindo para texto livre.", exc,
            )
            self.usage.record_schema_fallback()
            return None

        parsed = _parse_json_response(raw)
        if parsed is not None:
            return parsed

        # Resposta não parseável: uma re-tentativa antes de desistir das
        # garantias do schema (ver _log_malformed_json).
        self._log_malformed_json(retried=False)
        self._wait_if_rate_limited()
        try:
            raw = self._call_sync_inner(
                messages, _MALFORMED_JSON_RETRY_TEMPERATURE, max_tokens,
                thinking=False, schema=schema,
            )
        except Exception as exc:
            _log.warning("Re-tentativa do caminho JSON falhou (%s).", exc)
            self.usage.record_schema_fallback()
            return None

        parsed = _parse_json_response(raw)
        if parsed is None:
            self._log_malformed_json(retried=True)
        return parsed

    def _log_malformed_json(self, retried: bool) -> None:
        """Reporta que a resposta do caminho JSON não era um objeto parseável.

        Diferente do esgotamento de orçamento, aqui a chamada RETORNOU — o
        modelo apenas não produziu JSON válido (texto solto, JSON truncado, ou
        um valor que não é objeto). É estocástico, daí valer uma re-tentativa
        antes de desistir.

        Só contabiliza o fallback na desistência definitiva: um retry
        bem-sucedido preserva as garantias do schema e não é degradação.
        """
        if retried:
            _log.error(
                "Resposta não-JSON também na re-tentativa. O registro será "
                "gerado em TEXTO LIVRE, sem as garantias do schema (enum, "
                "minimum/maximum, additionalProperties)."
            )
            self.usage.record_schema_fallback()
        else:
            _log.warning(
                "Resposta do caminho JSON não é um objeto JSON válido — "
                "repetindo uma vez com temperature=%s.",
                _MALFORMED_JSON_RETRY_TEMPERATURE,
            )

    def _log_budget_exhausted(self, exc: Exception, retried: bool) -> None:
        """Reporta a desistência do caminho JSON por esgotamento de orçamento.

        Nível ERROR (não WARNING): diferente de "backend não suporta schema",
        esta é uma condição corrigível pelo operador, e o custo é duplo — os
        tokens já gastos são descartados E o registro perde as garantias
        estruturais do schema silenciosamente.
        """
        suffix = " mesmo após aumentar max_tokens" if retried else ""
        _log.error(
            "Caminho JSON abandonado%s (%s). O registro será gerado em TEXTO "
            "LIVRE, sem as garantias do schema (enum, minimum/maximum, "
            "additionalProperties). Aumente SYNESIS_CODER_MAX_TOKENS.",
            suffix, exc,
        )
        self.usage.record_schema_fallback()

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

        def _call_in_thread(
            tokens: int,
            force: Optional[int] = None,
            temp: Optional[float] = None,
        ) -> str:
            if self.recorder is not None:
                self.recorder.set_context(context)
            return self._call_sync_inner(
                messages, temperature if temp is None else temp, tokens,
                thinking=False, schema=schema, force_max_tokens=force,
            )

        try:
            raw = await asyncio.to_thread(_call_in_thread, max_tokens)
        except TokenBudgetExhausted as exc:
            retry_tokens = _retry_max_tokens(exc.max_tokens)
            if retry_tokens is None:
                self._log_budget_exhausted(exc, retried=False)
                return None
            _log.warning(
                "Orçamento de tokens esgotado no raciocínio (tipos: %s, "
                "max_tokens=%d) — repetindo o caminho JSON com max_tokens=%d.",
                exc.block_types, exc.max_tokens, retry_tokens,
            )
            await self._async_wait_if_rate_limited()
            try:
                raw = await asyncio.to_thread(
                    _call_in_thread, retry_tokens, retry_tokens
                )
            except Exception as exc2:
                self._log_budget_exhausted(exc2, retried=True)
                return None
        except Exception as exc:
            _log.warning(
                "Caminho JSON (async) falhou na chamada ao backend (%s) — "
                "caindo para texto livre.", exc,
            )
            self.usage.record_schema_fallback()
            return None

        parsed = _parse_json_response(raw)
        if parsed is not None:
            return parsed

        # Resposta não parseável: uma re-tentativa antes de desistir das
        # garantias do schema (ver _log_malformed_json).
        self._log_malformed_json(retried=False)
        await self._async_wait_if_rate_limited()
        try:
            raw = await asyncio.to_thread(
                _call_in_thread, max_tokens, None,
                _MALFORMED_JSON_RETRY_TEMPERATURE,
            )
        except Exception as exc:
            _log.warning("Re-tentativa do caminho JSON falhou (%s).", exc)
            self.usage.record_schema_fallback()
            return None

        parsed = _parse_json_response(raw)
        if parsed is None:
            self._log_malformed_json(retried=True)
        return parsed

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
        force_max_tokens: Optional[int] = None,
    ) -> str:
        """Lógica interna de chamada síncrona com retry (usada por call e call_async).

        Quando `schema` é fornecido, o modelo devolve JSON de valores (Opção 3):
        - Backend OpenAI-compatível: via `response_format` com `json_schema` strict.
        - Backend Anthropic: via `output_config.format` (structured outputs); o
          schema é saneado (`_sanitize_schema_for_anthropic`) para remover keywords
          não aceitos pelo constrained decoding.
        O parsing/validação fica a cargo do chamador (call_json).
        """
        from tenacity import retry, retry_if_exception_type, stop_after_attempt

        # Env override de temperatura — aplicado a todos os backends e modos analíticos.
        # Tem precedência sobre o argumento; None significa "deixar o modelo decidir".
        env_temp = _get_env_temperature()
        if env_temp is not None:
            temperature = env_temp

        # Precedência de max_tokens:
        #   0. force_max_tokens — usado pelo retry após TokenBudgetExhausted;
        #      precisa vencer o env, senão a re-tentativa repetiria o mesmo
        #      orçamento que acabou de estourar e falharia igual.
        #   1. SYNESIS_CODER_MAX_TOKENS (env)
        #   2. min(teto_via_API, estimativa_por_chunk) — dinâmico, padrão quando
        #      o chamador não passou valor explícito (max_tokens == _DEFAULT_MAX_TOKENS)
        #   3. Valor passado pelo chamador (ex.: suggest_mode com 512)
        env_max = None if force_max_tokens else _get_max_tokens_override()
        if force_max_tokens:
            max_tokens = force_max_tokens
        elif env_max is not None:
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
                    # Prompt caching é automático na maioria dos provedores
                    # OpenAI-compatíveis (OpenAI, DeepSeek, Grok, Moonshot,
                    # Z.AI) — não exige cache_control. O OpenRouter sempre
                    # inclui usage accounting. `prompt_tokens` já é o TOTAL e
                    # `cached_tokens` é subconjunto dele: por isso o
                    # acumulador NÃO recebe input_excludes_cache aqui.
                    details = getattr(response.usage, "prompt_tokens_details", None)
                    self.usage.record(
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        is_correction=is_correction,
                        cache_write_tok=_int_attr(details, "cache_write_tokens"),
                        cache_read_tok=_int_attr(details, "cached_tokens"),
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
                if schema is not None:
                    # Structured outputs (Anthropic): schema saneado para o
                    # constrained decoding. Erro/refusal/truncamento → o chamador
                    # (call_json) cai no texto livre via _parse_json_response=None.
                    kwargs["output_config"] = {
                        "format": {
                            "type": "json_schema",
                            "schema": _sanitize_schema_for_anthropic(schema),
                        }
                    }

                response = self._client.messages.create(**kwargs)
                self._record_usage(response.usage)

                if response.stop_reason == "refusal":
                    _log.debug(
                        "Anthropic recusou a geração estruturada (stop_reason=refusal) — "
                        "caindo para texto livre."
                    )
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

                block_types = [b.type for b in response.content]
                if response.stop_reason == "max_tokens":
                    # Orçamento esgotado antes de emitir texto: condição
                    # operacional corrigível (aumentar max_tokens), não
                    # limitação do backend. Exceção tipada permite ao chamador
                    # tratar os dois casos de forma diferente.
                    raise TokenBudgetExhausted(block_types, kwargs["max_tokens"])
                raise RuntimeError(
                    f"Resposta Anthropic sem bloco text — "
                    f"tipos recebidos: {block_types}"
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
        """Registra uso de tokens após chamada bem-sucedida (apenas Anthropic).

        `usage.input_tokens` na Anthropic é apenas o resto NÃO-cacheado; os
        tokens de cache vêm em campos separados e disjuntos. Por isso o
        acumulador recebe `input_excludes_cache=True` — ver docstring de
        token_usage.py.
        """
        now = time.monotonic()
        cache_write = _int_attr(usage, "cache_creation_input_tokens")
        cache_read = _int_attr(usage, "cache_read_input_tokens")
        # Rate limiting proativo conta o prompt inteiro (cache também consome
        # cota de input), não apenas o resto não-cacheado.
        self._request_times.append((now, 1))
        self._input_tokens.append((now, usage.input_tokens + cache_write + cache_read))
        self._output_tokens.append((now, usage.output_tokens))
        # Acumulador de sessao — le e reseta o flag de correcao da thread corrente
        is_correction = getattr(self._correction_local, "is_correction", False)
        self._correction_local.is_correction = False
        self.usage.record(
            usage.input_tokens,
            usage.output_tokens,
            is_correction=is_correction,
            cache_write_tok=cache_write,
            cache_read_tok=cache_read,
            input_excludes_cache=True,
        )

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

        Na maioria dos provedores OpenAI-compatíveis o prompt caching é
        AUTOMÁTICO por casamento de prefixo (OpenAI ≥1024 tokens, DeepSeek,
        Grok, Moonshot, Z.AI) — não há nada a marcar, e o campo "cache" do
        formato interno é corretamente ignorado.

        A exceção é a Anthropic servida via OpenRouter: ali o cache exige
        `cache_control` explícito no bloco de conteúdo, exatamente como na API
        nativa. Sem isso, um prefixo grande e estável (o system prompt do
        template) é reprocessado a preço cheio em toda chamada.

        Returns:
            ([], api_messages) — system_blocks sempre vazio; system incluso em api_messages
        """
        explicit_cache = _provider_requires_explicit_cache(self.model)
        api_messages = []
        for msg in messages:
            if explicit_cache and msg.get("cache"):
                api_messages.append({
                    "role": msg["role"],
                    "content": [{
                        "type": "text",
                        "text": msg["content"],
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
        return [], api_messages
