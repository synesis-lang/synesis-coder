"""Testes do caminho JSON (Opção 3) no llm_client — parsing e fallback (sem LLM)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from synesis_coder import llm_client as lc
from synesis_coder.llm_client import (
    LLMClient,
    _parse_json_response,
    _sanitize_schema_for_anthropic,
)


class TestParseJsonResponse:
    def test_plain_object(self):
        assert _parse_json_response('{"items": []}') == {"items": []}

    def test_fenced_json(self):
        assert _parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_lang(self):
        assert _parse_json_response('```\n{"a": 1}\n```') == {"a": 1}

    def test_invalid_returns_none(self):
        assert _parse_json_response("not json at all") is None

    def test_json_array_top_level_returns_none(self):
        # O contrato exige objeto no topo (envelope items); array puro → fallback.
        assert _parse_json_response("[1, 2, 3]") is None

    def test_empty_returns_none(self):
        assert _parse_json_response("") is None
        assert _parse_json_response("   ") is None


class TestSanitizeSchemaForAnthropic:
    def test_closed_integer_range_becomes_enum(self):
        """A Anthropic não suporta minimum/maximum, mas suporta enum.

        Converter preserva a restrição em vez de descartá-la — sem isso um campo
        ORDERED chegaria como `{"type": "integer"}` puro e o modelo poderia
        devolver qualquer inteiro.
        """
        schema = {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "multipleOf": 1,
        }
        assert _sanitize_schema_for_anthropic(schema) == {"enum": [1, 2, 3, 4, 5]}

    def test_nullable_range_keeps_null_in_the_enum(self):
        schema = {"type": ["integer", "null"], "minimum": 0, "maximum": 2}
        assert _sanitize_schema_for_anthropic(schema) == {"enum": [0, 1, 2, None]}

    def test_open_range_still_strips_to_plain_type(self):
        """Sem os dois limites não há lista finita a reconstruir."""
        assert _sanitize_schema_for_anthropic({"type": "integer", "minimum": 1}) == {
            "type": "integer"
        }

    def test_huge_range_is_not_expanded(self):
        """Um enum gigante incharia o prompt sem ganho prático."""
        schema = {"type": "integer", "minimum": 0, "maximum": 5000}
        assert _sanitize_schema_for_anthropic(schema) == {"type": "integer"}

    def test_recurses_into_properties_and_items(self):
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 10},
                "tags": {"type": "array", "items": {"type": "string", "minLength": 2}},
            },
            "additionalProperties": False,
            "required": ["score"],
        }
        out = _sanitize_schema_for_anthropic(schema)
        # Faixa fechada vira enum (restrição preservada); minLength é removido.
        assert out["properties"]["score"] == {"enum": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}
        assert out["properties"]["tags"]["items"] == {"type": "string"}
        # Keywords suportados preservados
        assert out["additionalProperties"] is False
        assert out["required"] == ["score"]

    def test_preserves_enum_const_and_structure(self):
        schema = {
            "enum": ["a", "b"],
            "const": "__untyped__",
            "minItems": 1,
        }
        # enum/const/minItems(=1) são suportados → intactos
        assert _sanitize_schema_for_anthropic(schema) == schema

    def test_does_not_mutate_original(self):
        schema = {"type": "integer", "minimum": 1}
        _sanitize_schema_for_anthropic(schema)
        assert schema == {"type": "integer", "minimum": 1}


class TestSupportsJsonSchema:
    def test_openai_always_true(self):
        client = LLMClient.__new__(LLMClient)
        client.backend = "openai"
        assert client.supports_json_schema() is True

    def test_anthropic_gated_on_sdk(self, monkeypatch):
        client = LLMClient.__new__(LLMClient)
        client.backend = "anthropic"

        monkeypatch.setattr(lc, "_anthropic_sdk_supports_output_config", lambda: True)
        assert client.supports_json_schema() is True

        monkeypatch.setattr(lc, "_anthropic_sdk_supports_output_config", lambda: False)
        assert client.supports_json_schema() is False

    def test_unknown_backend_false(self):
        client = LLMClient.__new__(LLMClient)
        client.backend = "somethingelse"
        assert client.supports_json_schema() is False


class TestAnthropicOutputConfig:
    """Ramo anthropic de _call_sync_inner monta output_config saneado (mock da API)."""

    def _make_client(self, monkeypatch):
        client = LLMClient.__new__(LLMClient)
        client.backend = "anthropic"
        client.model = "claude-sonnet-5"
        client.recorder = None
        client._model_output_cap = 0  # evita chamada de descoberta de teto
        client._correction_local = MagicMock(is_correction=False)
        # Mock do SDK anthropic: captura kwargs e devolve um bloco text com JSON.
        fake_resp = MagicMock()
        fake_resp.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"items": [{"code": ["x"]}]}'
        fake_resp.content = [text_block]
        fake_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        create = MagicMock(return_value=fake_resp)
        client._client = MagicMock()
        client._client.messages.create = create
        client._retryable_errors = (RuntimeError,)
        monkeypatch.setattr(client, "_record_usage", lambda usage: None)
        return client, create

    def test_output_config_built_and_sanitized(self, monkeypatch):
        client, create = self._make_client(monkeypatch)
        schema = {
            "type": "object",
            "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5}},
            "additionalProperties": False,
        }
        result = client._call_sync_inner(
            [{"role": "user", "content": "x", "cache": False}],
            temperature=0.0,
            max_tokens=1024,
            thinking=False,
            schema=schema,
        )
        assert result == '{"items": [{"code": ["x"]}]}'
        kwargs = create.call_args.kwargs
        assert "output_config" in kwargs
        fmt = kwargs["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        # Faixa fechada vira enum no wire (restrição preservada, já que a
        # Anthropic não suporta minimum/maximum); additionalProperties intacto.
        assert fmt["schema"]["properties"]["score"] == {"enum": [1, 2, 3, 4, 5]}
        assert fmt["schema"]["additionalProperties"] is False

    def test_no_output_config_when_schema_none(self, monkeypatch):
        client, create = self._make_client(monkeypatch)
        client._call_sync_inner(
            [{"role": "user", "content": "x", "cache": False}],
            temperature=0.0,
            max_tokens=1024,
            thinking=False,
            schema=None,
        )
        assert "output_config" not in create.call_args.kwargs


class TestEmptyProviderResponse:
    """`choices: null` é falha transitória do provedor, não limitação do backend.

    O OpenRouter repassa recusas do provedor final dentro de um 200 OK. Sem
    guarda, `response.choices[0]` estourava `'NoneType' object is not
    subscriptable` — erro críptico que o chamador tratava como "backend não
    suporta schema", descartando as garantias do schema por engano.
    """

    def _client(self, monkeypatch, responses):
        from synesis_coder import llm_client as mod

        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "openai")
        monkeypatch.setenv("SYNESIS_CODER_API_KEY", "k")
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "m")

        calls = {"n": 0}

        def create(**kwargs):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            if isinstance(r, Exception):
                raise r
            return r

        client = mod.LLMClient()
        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        return client, calls

    def _ok_response(self, content: str):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=None,
        )

    def _empty_response(self):
        return SimpleNamespace(choices=None, usage=None, error={"code": 502})

    def test_raises_typed_error_instead_of_typeerror(self, monkeypatch):
        from synesis_coder.llm_client import EmptyProviderResponse

        client, _ = self._client(monkeypatch, [self._empty_response()])
        with pytest.raises(EmptyProviderResponse) as excinfo:
            client._call_sync_inner(
                [{"role": "user", "content": "x", "cache": False}],
                temperature=0.0,
                max_tokens=100,
                thinking=False,
                schema=None,
            )
        assert "choices" in str(excinfo.value)

    def test_error_payload_is_reported(self, monkeypatch):
        from synesis_coder.llm_client import EmptyProviderResponse

        client, _ = self._client(monkeypatch, [self._empty_response()])
        with pytest.raises(EmptyProviderResponse) as excinfo:
            client._call_sync_inner(
                [{"role": "user", "content": "x", "cache": False}],
                temperature=0.0, max_tokens=100, thinking=False, schema=None,
            )
        assert "502" in str(excinfo.value)

    def test_call_json_retries_once_and_succeeds(self, monkeypatch):
        """Uma falha transitória não deve custar as garantias do schema."""
        client, calls = self._client(
            monkeypatch,
            [self._empty_response(), self._ok_response('{"a": 1}')],
        )
        result = client.call_json(
            [{"role": "user", "content": "x", "cache": False}],
            schema={"type": "object"},
        )
        assert result == {"a": 1}
        assert calls["n"] == 2  # falhou, repetiu, funcionou

    def test_call_json_gives_up_after_second_failure(self, monkeypatch):
        client, calls = self._client(
            monkeypatch, [self._empty_response(), self._empty_response()]
        )
        assert client.call_json(
            [{"role": "user", "content": "x", "cache": False}],
            schema={"type": "object"},
        ) is None
        assert calls["n"] == 2

    def test_empty_choices_list_is_also_caught(self, monkeypatch):
        from synesis_coder.llm_client import EmptyProviderResponse

        resp = SimpleNamespace(choices=[], usage=None, error=None)
        client, _ = self._client(monkeypatch, [resp])
        with pytest.raises(EmptyProviderResponse):
            client._call_sync_inner(
                [{"role": "user", "content": "x", "cache": False}],
                temperature=0.0, max_tokens=100, thinking=False, schema=None,
            )
