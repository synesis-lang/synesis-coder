"""Testes para TokenUsage e marcacao de correcoes no LLMClient.

Todos os testes sao unitarios — sem LLM, sem IO.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from synesis_coder.token_usage import TokenUsage


class TestTokenUsageRecord:
    def test_record_accumulates(self):
        """Multiplas chamadas a record() somam corretamente."""
        u = TokenUsage()
        u.record(1000, 200)
        u.record(500, 100)
        u.record(300, 50)

        assert u.input_tokens == 1800
        assert u.output_tokens == 350
        assert u.api_calls == 3
        assert u.corrections == 0

    def test_total_tokens_property(self):
        """total_tokens == input_tokens + output_tokens."""
        u = TokenUsage()
        u.record(1200, 340)

        assert u.total_tokens == 1540

    def test_correction_flag_increments_only_when_true(self):
        """corrections so incrementa quando is_correction=True."""
        u = TokenUsage()
        u.record(100, 50, is_correction=False)
        u.record(100, 50, is_correction=True)
        u.record(100, 50, is_correction=False)
        u.record(100, 50, is_correction=True)

        assert u.corrections == 2
        assert u.api_calls == 4


class TestTokenUsageSummaryLine:
    def test_summary_line_format_no_corrections(self):
        """Linha formatada contem in, out, total, calls — sem corrections."""
        u = TokenUsage()
        u.record(4231, 312)
        line = u.summary_line()

        assert "in 4,231" in line
        assert "out 312" in line
        assert "total 4,543" in line
        assert "calls 1" in line
        assert "corrections" not in line

    def test_summary_line_format_with_corrections(self):
        """Inclui 'corrections N' quando corrections > 0."""
        u = TokenUsage()
        u.record(4000, 300)
        u.record(500, 80, is_correction=True)
        line = u.summary_line()

        assert "corrections 1" in line

    def test_summary_line_zero_tokens(self):
        """Funciona com acumulador zerado."""
        u = TokenUsage()
        line = u.summary_line()

        assert "in 0" in line
        assert "out 0" in line
        assert "total 0" in line
        assert "calls 0" in line


class TestTokenUsageCache:
    """Acumulacao e exibicao de metricas de prompt caching."""

    def test_cache_kwargs_default_to_zero(self):
        """Chamadores que nao informam cache continuam funcionando (compat)."""
        u = TokenUsage()
        u.record(1000, 200)

        assert u.cache_write_tokens == 0
        assert u.cache_read_tokens == 0

    def test_cache_tokens_accumulate(self):
        """cache_write/cache_read somam ao longo das chamadas."""
        u = TokenUsage()
        u.record(100, 50, cache_write_tok=4000, cache_read_tok=0)
        u.record(80, 40, cache_write_tok=0, cache_read_tok=4000)
        u.record(80, 40, cache_write_tok=0, cache_read_tok=4000)

        assert u.cache_write_tokens == 4000
        assert u.cache_read_tokens == 8000

    def test_anthropic_total_sums_cache_fields(self):
        """Anthropic: input_tokens exclui cache, entao o total soma os tres."""
        u = TokenUsage()
        u.record(100, 50, cache_write_tok=0, cache_read_tok=4000,
                 input_excludes_cache=True)

        assert u.total_prompt_tokens == 4100
        assert u.total_tokens == 4150

    def test_openai_total_does_not_double_count(self):
        """OpenAI-compat: prompt_tokens ja e o total; cache nao soma de novo."""
        u = TokenUsage()
        u.record(4100, 50, cache_read_tok=4000)  # sem input_excludes_cache

        assert u.total_prompt_tokens == 4100
        assert u.total_tokens == 4150

    def test_summary_line_omits_cache_when_absent(self):
        """Sem atividade de cache a linha permanece enxuta."""
        u = TokenUsage()
        u.record(1000, 200)

        assert "cache" not in u.summary_line()

    def test_summary_line_shows_cache_when_present(self):
        """Com atividade de cache o segmento aparece."""
        u = TokenUsage()
        u.record(100, 50, cache_write_tok=1200, cache_read_tok=3400,
                 input_excludes_cache=True)
        line = u.summary_line()

        assert "cache w 1,200/r 3,400" in line

    def test_summary_line_shows_cache_with_only_reads(self):
        """Cache read sem write (prefixo ja quente) tambem exibe."""
        u = TokenUsage()
        u.record(100, 50, cache_read_tok=4000, input_excludes_cache=True)

        assert "cache w 0/r 4,000" in u.summary_line()


class TestTokenUsageReset:
    def test_reset_zeroes_all_fields(self):
        """reset() zera todos os contadores."""
        u = TokenUsage()
        u.record(1000, 200, is_correction=True,
                 cache_write_tok=500, cache_read_tok=300,
                 input_excludes_cache=True)
        u.reset()

        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_write_tokens == 0
        assert u.cache_read_tokens == 0
        assert u.api_calls == 0
        assert u.corrections == 0
        assert u.total_tokens == 0


class TestTokenUsageThreadSafety:
    def test_concurrent_record_no_race_condition(self):
        """10 threads chamando record() simultaneamente acumulam corretamente."""
        u = TokenUsage()
        n_threads = 10
        calls_per_thread = 100

        def _worker():
            for _ in range(calls_per_thread):
                u.record(10, 5)

        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_calls = n_threads * calls_per_thread
        assert u.api_calls == expected_calls
        assert u.input_tokens == expected_calls * 10
        assert u.output_tokens == expected_calls * 5


# ---------------------------------------------------------------------------
# Testes de marcacao de correcoes no LLMClient (Fase 3)
# ---------------------------------------------------------------------------


def _make_mock_anthropic_response(input_tokens: int, output_tokens: int) -> MagicMock:
    """Cria resposta mock da API Anthropic com usage."""
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ITEM @ref\nEND ITEM"
    response.content = [text_block]
    response.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return response


class TestLLMClientCorrectionFlag:
    """Verifica que fix()/fix_async() marcam correcoes no acumulador."""

    def _make_client(self) -> "LLMClient":
        """Instancia LLMClient com backend Anthropic mockado (sem API key real)."""
        from synesis_coder.llm_client import LLMClient

        with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="test-key"):
            client = LLMClient(model="claude-sonnet-4-6", backend="anthropic")
        # Substituir o cliente interno por um mock controlavel
        client._client = MagicMock()
        client._rate_limit_enabled = False  # desabilitar rate limiting
        return client

    def test_call_does_not_mark_correction(self):
        """call() normal nao incrementa corrections."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(500, 100)

        client.call([{"role": "user", "content": "hello", "cache": False}])

        assert client.usage.corrections == 0
        assert client.usage.api_calls == 1

    def test_fix_marks_correction(self):
        """fix() marca a chamada como correcao."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(400, 80)

        client.fix("bad output", "error msg")

        assert client.usage.corrections == 1
        assert client.usage.api_calls == 1

    def test_call_then_fix_counts_correctly(self):
        """Uma call() seguida de fix() resulta em api_calls=2, corrections=1."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(500, 100)

        client.call([{"role": "user", "content": "generate", "cache": False}])
        client.fix("bad output", "error msg")

        assert client.usage.api_calls == 2
        assert client.usage.corrections == 1
        assert client.usage.input_tokens == 1000
        assert client.usage.output_tokens == 200

    def test_fix_flag_resets_after_use(self):
        """Apos fix(), a proxima call() nao e marcada como correcao."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(300, 60)

        client.fix("bad output", "error")
        client.call([{"role": "user", "content": "next call", "cache": False}])

        assert client.usage.api_calls == 2
        assert client.usage.corrections == 1  # apenas a primeira

    def test_fix_async_marks_correction(self):
        """fix_async() marca a chamada como correcao."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(400, 80)

        asyncio.run(client.fix_async("bad output", "error msg"))

        assert client.usage.corrections == 1
        assert client.usage.api_calls == 1

    def test_concurrent_fix_async_no_flag_collision(self):
        """Multiplos fix_async() concorrentes nao colidem no flag de correcao."""
        client = self._make_client()
        client._client.messages.create.return_value = _make_mock_anthropic_response(200, 50)

        async def _run():
            tasks = [
                client.fix_async("bad output", f"error {i}")
                for i in range(5)
            ]
            await asyncio.gather(*tasks)

        asyncio.run(_run())

        # Todas as 5 chamadas devem ser marcadas como correcao
        assert client.usage.api_calls == 5
        assert client.usage.corrections == 5


# ---------------------------------------------------------------------------
# Leitura das metricas de cache nos dois backends
# ---------------------------------------------------------------------------


class TestCacheMetricsCapture:
    """Verifica que os campos de cache sao lidos da resposta de cada backend."""

    def _make_anthropic_client(self):
        from synesis_coder.llm_client import LLMClient

        with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="test-key"):
            client = LLMClient(model="claude-sonnet-4-6", backend="anthropic")
        client._client = MagicMock()
        client._rate_limit_enabled = False
        return client

    def test_anthropic_reads_cache_fields(self):
        """Branch Anthropic captura cache_creation/cache_read_input_tokens."""
        client = self._make_anthropic_client()
        response = _make_mock_anthropic_response(120, 60)
        response.usage = SimpleNamespace(
            input_tokens=120,
            output_tokens=60,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=4096,
        )
        client._client.messages.create.return_value = response

        client.call([{"role": "user", "content": "x", "cache": False}])

        assert client.usage.cache_read_tokens == 4096
        assert client.usage.cache_write_tokens == 0
        # input_tokens da Anthropic exclui cache -> total soma os dois
        assert client.usage.total_prompt_tokens == 120 + 4096

    def test_anthropic_usage_without_cache_fields_is_safe(self):
        """Usage sem os campos de cache (MagicMock) nao quebra nem polui."""
        client = self._make_anthropic_client()
        # _make_mock_anthropic_response usa MagicMock: getattr devolveria um
        # MagicMock, nao 0 — o helper _int_attr precisa coagir para int.
        client._client.messages.create.return_value = _make_mock_anthropic_response(500, 100)

        client.call([{"role": "user", "content": "x", "cache": False}])

        assert client.usage.cache_write_tokens == 0
        assert client.usage.cache_read_tokens == 0
        assert client.usage.total_tokens == 600

    def test_openai_reads_prompt_tokens_details(self):
        """Branch OpenAI captura prompt_tokens_details.cached_tokens."""
        from synesis_coder.llm_client import LLMClient

        with patch("synesis_coder.llm_client._get_api_url", return_value="http://x"), \
             patch("synesis_coder.llm_client._get_api_key", return_value="key"):
            client = LLMClient(model="gpt-5.6-luna", backend="openai")
        client._client = MagicMock()
        client._rate_limit_enabled = False

        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ITEM @r\nEND ITEM"),
        )
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(
                prompt_tokens=5000,
                completion_tokens=120,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=4096, cache_write_tokens=0,
                ),
            ),
        )

        client.call([{"role": "user", "content": "x", "cache": False}])

        assert client.usage.cache_read_tokens == 4096
        # prompt_tokens ja e o total -> nao pode dobrar a contagem
        assert client.usage.total_prompt_tokens == 5000

    def test_openai_without_details_is_safe(self):
        """Provedor que nao preenche prompt_tokens_details nao quebra."""
        from synesis_coder.llm_client import LLMClient

        with patch("synesis_coder.llm_client._get_api_url", return_value="http://x"), \
             patch("synesis_coder.llm_client._get_api_key", return_value="key"):
            client = LLMClient(model="some-model", backend="openai")
        client._client = MagicMock()
        client._rate_limit_enabled = False

        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ok"),
        )
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40),
        )

        client.call([{"role": "user", "content": "x", "cache": False}])

        assert client.usage.cache_read_tokens == 0
        assert client.usage.total_prompt_tokens == 300
