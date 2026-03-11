"""Cliente LLM para o synesis-coder.

Única classe que importa `anthropic`. Isola o acoplamento ao provedor —
prompt_builder e demais módulos usam formato interno agnóstico.

Formato interno de mensagens (agnóstico ao provedor):
    [
        {"role": "system", "content": str, "cache": bool},
        {"role": "user",   "content": str, "cache": bool},
    ]

LLMClient traduz para o formato Anthropic internamente.
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import List, Optional

from dotenv import load_dotenv

# Carrega .env (variáveis de ambiente têm precedência)
load_dotenv()


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY não encontrada. "
            "Crie um arquivo .env baseado em .env.example e defina sua chave."
        )
    return key


def _get_model() -> str:
    return os.environ.get("SYNESIS_CODER_MODEL", "claude-opus-4-6")


def _get_max_retries() -> int:
    return int(os.environ.get("SYNESIS_CODER_MAX_RETRIES", "3"))


class LLMClient:
    """Cliente síncrono para chamadas à API Anthropic com rate limiting.

    Gerencia:
    - Rate limiting por RPM e TPM (janela deslizante de 60s)
    - Sleep proativo antes de estourar cotas (margem de 15%)
    - Retry com tenacity em erros 429/503
    """

    # Limites padrão conservadores para Tier 1 da Anthropic
    _DEFAULT_MAX_RPM = 50
    _DEFAULT_MAX_INPUT_TPM = 40_000
    _DEFAULT_MAX_OUTPUT_TPM = 8_000
    _SAFETY_MARGIN = 0.85  # usar até 85% da cota antes de pausar

    def __init__(
        self,
        model: Optional[str] = None,
        max_rpm: Optional[int] = None,
        max_input_tpm: Optional[int] = None,
        max_output_tpm: Optional[int] = None,
    ) -> None:
        """Inicializa o cliente.

        Args:
            model: ID do modelo (padrão: env SYNESIS_CODER_MODEL ou claude-opus-4-6).
            max_rpm: Limite de requisições por minuto.
            max_input_tpm: Limite de tokens de input por minuto.
            max_output_tpm: Limite de tokens de output por minuto.
        """
        import anthropic

        self._client = anthropic.Anthropic(api_key=_get_api_key())
        self.model = model or _get_model()

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

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def call(
        self,
        messages: List[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Envia mensagens ao LLM e retorna o texto de resposta.

        Args:
            messages: Lista no formato interno:
                [{"role": "system"|"user"|"assistant", "content": str, "cache": bool}]
            temperature: Temperatura da geração (0 = determinístico).
            max_tokens: Máximo de tokens de output.

        Returns:
            Texto da resposta do assistente.
        """
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        import anthropic

        self._wait_if_rate_limited()

        system_blocks, api_messages = self._translate_messages(messages)

        @retry(
            retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
            stop=stop_after_attempt(_get_max_retries()),
            wait=wait_exponential(multiplier=2, min=4, max=60),
            reraise=True,
        )
        def _call_with_retry() -> str:
            kwargs: dict = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": api_messages,
            }
            if system_blocks:
                kwargs["system"] = system_blocks

            response = self._client.messages.create(**kwargs)
            self._record_usage(response.usage)
            return response.content[0].text

        return _call_with_retry()

    def fix(
        self,
        previous_output: str,
        errors: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
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
                    "O output gerado contém erros de sintaxe Synesis. "
                    "Corrija-o para que seja válido.\n\n"
                    "OUTPUT COM ERROS:\n"
                    f"```\n{previous_output}\n```\n\n"
                    "ERROS DO COMPILADOR:\n"
                    f"{errors}\n\n"
                    "Gere apenas o output corrigido, sem explicações."
                ),
                "cache": False,
            }
        ]
        return self.call(fix_messages, temperature=temperature, max_tokens=max_tokens)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _wait_if_rate_limited(self) -> None:
        """Pausa proativamente se próxima chamada ultrapassaria as cotas."""
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
        """Registra uso de tokens após chamada bem-sucedida."""
        now = time.monotonic()
        self._request_times.append((now, 1))
        self._input_tokens.append((now, usage.input_tokens))
        self._output_tokens.append((now, usage.output_tokens))

    # ------------------------------------------------------------------
    # Tradução de formato interno → Anthropic
    # ------------------------------------------------------------------

    def _translate_messages(
        self, messages: List[dict]
    ) -> tuple[list, list]:
        """Converte formato interno para system_blocks + messages da API Anthropic.

        Returns:
            (system_blocks, api_messages)
        """
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
