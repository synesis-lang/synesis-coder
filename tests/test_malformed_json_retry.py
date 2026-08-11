"""Reprocessamento automático quando a resposta do caminho JSON não é parseável.

Antes desta garantia, uma resposta não-JSON fazia `call_json` devolver None sem
re-tentar E sem contabilizar o fallback: o registro era gerado em texto livre,
sem as restrições do schema, e nada no pipeline registrava a degradação — nem
mesmo o aviso de fim de execução, que lê `usage.schema_fallbacks`.

Diferente do esgotamento de orçamento (condição do ambiente), uma resposta
malformada é estocástica: repetir com temperatura levemente acima de 0 tem
chance real de recuperar o caminho JSON.

Nenhum teste aqui chama a API.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from synesis_coder.llm_client import (
    _MALFORMED_JSON_RETRY_TEMPERATURE,
    LLMClient,
)
from synesis_coder.token_usage import TokenUsage

_MESSAGES = [{"role": "user", "content": "x", "cache": False}]
_SCHEMA = {"type": "object"}


def _client(responses: list[str]) -> tuple[LLMClient, list]:
    """Cliente com `_call_sync_inner` mockado, devolvendo `responses` em ordem.

    Registra as chamadas em `calls` como (temperature, max_tokens) para que os
    testes verifiquem a temperatura da re-tentativa.
    """
    client = LLMClient.__new__(LLMClient)
    client.backend = "anthropic"
    client.model = "claude-sonnet-5"
    client.recorder = None
    client.usage = TokenUsage()
    client._correction_local = MagicMock(is_correction=False)

    calls: list = []
    pending = list(responses)

    def _inner(messages, temperature, max_tokens, thinking=True,
               thinking_budget=None, schema=None, force_max_tokens=None):
        calls.append((temperature, max_tokens))
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client._call_sync_inner = _inner  # type: ignore[method-assign]
    client.supports_json_schema = lambda: True  # type: ignore[method-assign]
    client._wait_if_rate_limited = lambda: None  # type: ignore[method-assign]

    async def _noop() -> None:
        return None

    client._async_wait_if_rate_limited = _noop  # type: ignore[method-assign]
    return client, calls


class TestCallJsonMalformedRetry:
    def test_retries_once_and_recovers(self):
        client, calls = _client(["not json", '{"items": []}'])

        result = client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert result == {"items": []}
        assert len(calls) == 2

    def test_successful_retry_is_not_counted_as_fallback(self):
        """Recuperar o caminho JSON preserva o schema — não é degradação."""
        client, _ = _client(["not json", '{"items": []}'])

        client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert client.usage.schema_fallbacks == 0

    def test_retry_uses_elevated_temperature(self):
        """temperature=0.0 repetiria a mesma saída malformada."""
        client, calls = _client(["not json", '{"a": 1}'])

        client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert calls[0][0] == 0.0
        assert calls[1][0] == _MALFORMED_JSON_RETRY_TEMPERATURE

    def test_gives_up_after_one_retry_and_counts_fallback(self):
        client, calls = _client(["not json", "still not json"])

        result = client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert result is None
        assert len(calls) == 2, "deve re-tentar exatamente uma vez"
        assert client.usage.schema_fallbacks == 1

    def test_retry_exception_counts_fallback(self):
        client, _ = _client(["not json", RuntimeError("boom")])

        result = client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert result is None
        assert client.usage.schema_fallbacks == 1

    def test_valid_first_response_does_not_retry(self):
        """Caminho feliz inalterado: uma chamada, sem custo extra."""
        client, calls = _client(['{"items": [1]}'])

        result = client.call_json(_MESSAGES, _SCHEMA, temperature=0.0)

        assert result == {"items": [1]}
        assert len(calls) == 1
        assert client.usage.schema_fallbacks == 0


class TestCallJsonAsyncMalformedRetry:
    def test_retries_once_and_recovers(self):
        client, calls = _client(["not json", '{"items": []}'])

        result = asyncio.run(
            client.call_json_async(_MESSAGES, _SCHEMA, temperature=0.0)
        )

        assert result == {"items": []}
        assert len(calls) == 2

    def test_retry_uses_elevated_temperature(self):
        client, calls = _client(["not json", '{"a": 1}'])

        asyncio.run(client.call_json_async(_MESSAGES, _SCHEMA, temperature=0.0))

        assert calls[1][0] == _MALFORMED_JSON_RETRY_TEMPERATURE

    def test_gives_up_after_one_retry_and_counts_fallback(self):
        client, calls = _client(["not json", "still not json"])

        result = asyncio.run(
            client.call_json_async(_MESSAGES, _SCHEMA, temperature=0.0)
        )

        assert result is None
        assert len(calls) == 2
        assert client.usage.schema_fallbacks == 1

    def test_valid_first_response_does_not_retry(self):
        client, calls = _client(['{"items": [1]}'])

        result = asyncio.run(
            client.call_json_async(_MESSAGES, _SCHEMA, temperature=0.0)
        )

        assert result == {"items": [1]}
        assert len(calls) == 1
