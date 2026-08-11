"""Testes das correções de robustez do caminho JSON e do loop de correção.

Cobre quatro defeitos distintos:

1. Caminho JSON degradava para texto livre em SILÊNCIO quando o modelo gastava
   todo o `max_tokens` pensando (só ThinkingBlock, sem bloco `text`). O WARNING
   genérico se confundia com "backend não suporta schema", que exige ação
   oposta. Agora: exceção tipada, retry com orçamento maior, log em ERROR.
2. Os fallbacks de schema não eram contabilizados — em lote, alguns registros
   rodavam com schema e outros sem, e a diferença não aparecia em lugar nenhum.
3. O loop de correção podia TRUNCAR o arquivo (caso documentado: 19 ITEMs → 1,
   com perda do SOURCE) sem nada detectar, porque a saída seguia válida.
4. Anthropic via OpenRouter exige `cache_control` explícito, que era descartado.

Todos offline — sem LLM, sem IO de rede.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from synesis_coder.llm_client import (
    LLMClient,
    TokenBudgetExhausted,
    _provider_requires_explicit_cache,
    _retry_max_tokens,
)


def _client(backend: str = "anthropic", model: str | None = None) -> LLMClient:
    if backend == "anthropic":
        with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="k"):
            c = LLMClient(model=model or "claude-sonnet-4-6", backend="anthropic")
    else:
        with patch("synesis_coder.llm_client._get_api_url", return_value="http://x"), \
             patch("synesis_coder.llm_client._get_api_key", return_value="k"):
            c = LLMClient(model=model or "gpt-5.6-luna", backend="openai")
    c._client = MagicMock()
    c._rate_limit_enabled = False
    return c


def _resp(block_types: list[str], stop_reason: str, text: str = "ok") -> MagicMock:
    r = MagicMock()
    blocks = []
    for t in block_types:
        b = MagicMock()
        b.type = t
        if t == "text":
            b.text = text
        blocks.append(b)
    r.content = blocks
    r.stop_reason = stop_reason
    r.usage = MagicMock(
        input_tokens=100, output_tokens=200,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return r


# ---------------------------------------------------------------------------
# 1. Esgotamento de orçamento: exceção tipada + retry
# ---------------------------------------------------------------------------


class TestTokenBudgetExhausted:
    def test_raises_typed_exception_when_truncated_without_text(self):
        """Só thinking + stop_reason=max_tokens → exceção TIPADA."""
        c = _client()
        c._client.messages.create.return_value = _resp(["thinking"], "max_tokens")

        with pytest.raises(TokenBudgetExhausted) as ei:
            c.call([{"role": "user", "content": "x", "cache": False}])

        assert ei.value.block_types == ["thinking"]
        assert ei.value.max_tokens > 0

    def test_generic_runtime_error_when_not_truncated(self):
        """Sem bloco text mas SEM truncagem → erro genérico (causa diferente)."""
        c = _client()
        c._client.messages.create.return_value = _resp(["thinking"], "end_turn")

        with pytest.raises(RuntimeError) as ei:
            c.call([{"role": "user", "content": "x", "cache": False}])

        assert not isinstance(ei.value, TokenBudgetExhausted)

    def test_retry_max_tokens_doubles(self):
        assert _retry_max_tokens(16000) == 32000

    def test_retry_max_tokens_respects_cap(self):
        assert _retry_max_tokens(50000) == 64000

    def test_retry_max_tokens_gives_up_at_cap(self):
        """No teto, devolve None — não insiste numa chamada que falharia igual."""
        assert _retry_max_tokens(64000) is None

    def test_call_json_retries_with_larger_budget(self):
        """call_json repete com o dobro do orçamento antes de desistir."""
        c = _client()
        c._client.messages.create.side_effect = [
            _resp(["thinking"], "max_tokens"),
            _resp(["thinking", "text"], "end_turn", '{"items":[]}'),
        ]

        with patch.object(c, "supports_json_schema", return_value=True):
            out = c.call_json(
                [{"role": "user", "content": "x", "cache": False}],
                {"type": "object"}, max_tokens=16000,
            )

        assert out == {"items": []}, "retry deveria recuperar o caminho JSON"
        assert c._client.messages.create.call_count == 2
        second = c._client.messages.create.call_args_list[1].kwargs
        assert second["max_tokens"] == 32000

    def test_call_json_gives_up_after_failed_retry(self):
        """Se o retry também falhar, devolve None (fallback preservado)."""
        c = _client()
        c._client.messages.create.side_effect = [
            _resp(["thinking"], "max_tokens"),
            _resp(["thinking"], "max_tokens"),
        ]

        with patch.object(c, "supports_json_schema", return_value=True):
            out = c.call_json(
                [{"role": "user", "content": "x", "cache": False}],
                {"type": "object"}, max_tokens=16000,
            )

        assert out is None
        assert c.usage.schema_fallbacks == 1

    def test_call_json_async_retries_too(self):
        """O caminho async tem a mesma proteção."""
        c = _client()
        c._client.messages.create.side_effect = [
            _resp(["thinking"], "max_tokens"),
            _resp(["thinking", "text"], "end_turn", '{"items":[]}'),
        ]

        with patch.object(c, "supports_json_schema", return_value=True):
            out = asyncio.run(c.call_json_async(
                [{"role": "user", "content": "x", "cache": False}],
                {"type": "object"}, max_tokens=16000,
            ))

        assert out == {"items": []}
        assert c._client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# 2. Contabilização dos fallbacks de schema
# ---------------------------------------------------------------------------


class TestSchemaFallbackAccounting:
    def test_counter_starts_at_zero_and_is_hidden(self):
        from synesis_coder.token_usage import TokenUsage

        u = TokenUsage()
        u.record(100, 50)
        assert u.schema_fallbacks == 0
        assert "schema-fallback" not in u.summary_line()

    def test_counter_appears_in_summary_when_nonzero(self):
        from synesis_coder.token_usage import TokenUsage

        u = TokenUsage()
        u.record(100, 50)
        u.record_schema_fallback()
        u.record_schema_fallback()

        assert u.schema_fallbacks == 2
        assert "schema-fallbacks 2" in u.summary_line()

    def test_generic_backend_failure_also_counted(self):
        """Qualquer queda para texto livre conta, não só a por orçamento."""
        c = _client()
        c._client.messages.create.side_effect = RuntimeError("400 bad schema")

        with patch.object(c, "supports_json_schema", return_value=True):
            assert c.call_json([{"role": "user", "content": "x", "cache": False}],
                               {"type": "object"}) is None

        assert c.usage.schema_fallbacks == 1

    def test_reset_clears_counter(self):
        from synesis_coder.token_usage import TokenUsage

        u = TokenUsage()
        u.record_schema_fallback()
        u.reset()
        assert u.schema_fallbacks == 0


# ---------------------------------------------------------------------------
# 3. Guarda de idempotência do loop de correção
# ---------------------------------------------------------------------------


class TestFixIdempotencyGuard:
    def test_rejects_item_loss(self):
        """O caso documentado: muitos ITEMs → um só."""
        from synesis_coder.validator import _accept_fix

        prev = "ITEM @a\nEND ITEM\nITEM @b\nEND ITEM\nITEM @c\nEND ITEM"
        out, ok = _accept_fix(prev, "ITEM @a\nEND ITEM")

        assert ok is False
        assert out == prev, "deve preservar a versão anterior intacta"

    def test_rejects_source_loss(self):
        """SOURCE que existia e sumiu é truncagem, não correção."""
        from synesis_coder.validator import _accept_fix

        prev = "SOURCE @s\nEND SOURCE\nITEM @a\nEND ITEM"
        out, ok = _accept_fix(prev, "ITEM @a\nEND ITEM")

        assert ok is False
        assert out == prev

    def test_rejects_empty_candidate(self):
        from synesis_coder.validator import _accept_fix

        prev = "ITEM @a\nEND ITEM"
        assert _accept_fix(prev, "   ")[1] is False

    def test_accepts_same_count(self):
        """Correção que conserta sem perder blocos passa."""
        from synesis_coder.validator import _accept_fix

        prev = "ITEM @a\n  score: 9\nEND ITEM"
        cand = "ITEM @a\n  score: 3\nEND ITEM"
        out, ok = _accept_fix(prev, cand)

        assert ok is True
        assert out == cand

    def test_accepts_more_items(self):
        """Dividir um ITEM malformado em dois é resultado legítimo."""
        from synesis_coder.validator import _accept_fix

        prev = "ITEM @a\nEND ITEM"
        cand = "ITEM @a\nEND ITEM\nITEM @b\nEND ITEM"

        assert _accept_fix(prev, cand)[1] is True

    def test_guard_active_in_validate_and_fix(self):
        """O laço real mantém a versão anterior quando o fix trunca."""
        from synesis_coder import validator

        client = MagicMock()
        client.fix.return_value = "ITEM @a\nEND ITEM"  # 1 de 3 → truncagem
        prev = "ITEM @a\nEND ITEM\nITEM @b\nEND ITEM\nITEM @c\nEND ITEM"

        with patch.object(validator, "synesis") as ms, \
             patch.object(validator, "_fix_system_prompt", return_value="S"), \
             patch.object(validator, "_has_structural_errors", return_value=True), \
             patch.object(validator, "_extract_item_blocks", side_effect=lambda t: t):
            ms.load.return_value = MagicMock(
                get_diagnostics=MagicMock(return_value="E017")
            )
            final, ok = validator.validate_and_fix(
                prev, {"project_content": "P", "template_content": "T"},
                client, max_tries=1,
            )

        assert ok is False
        assert final.count("ITEM @") == 3, "os 3 ITEMs devem sobreviver à truncagem"


# ---------------------------------------------------------------------------
# 4. cache_control para Anthropic via OpenRouter
# ---------------------------------------------------------------------------


class TestExplicitCacheForOpenRouter:
    @pytest.mark.parametrize("model,expected", [
        ("anthropic/claude-sonnet-4-6", True),
        ("qwen/qwen3-max", True),
        ("openai/gpt-5.6-luna", False),
        ("deepseek/deepseek-r1", False),
        ("x-ai/grok-4", False),
    ])
    def test_provider_detection(self, model, expected):
        assert _provider_requires_explicit_cache(model) is expected

    def test_anthropic_via_openrouter_gets_cache_control(self):
        """O bloco marcado cache:True vira content-block com cache_control."""
        c = _client("openai", model="anthropic/claude-sonnet-4-6")
        _, msgs = c._translate_messages_openai(
            [{"role": "system", "content": "GUIDELINES", "cache": True},
             {"role": "user", "content": "x", "cache": False}]
        )

        assert isinstance(msgs[0]["content"], list)
        assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        # A mensagem não-cacheável segue string simples
        assert isinstance(msgs[1]["content"], str)

    def test_automatic_cache_providers_unchanged(self):
        """Provedor com cache automático não recebe marcação (seria inócua)."""
        c = _client("openai", model="openai/gpt-5.6-luna")
        _, msgs = c._translate_messages_openai(
            [{"role": "system", "content": "GUIDELINES", "cache": True}]
        )

        assert isinstance(msgs[0]["content"], str)
