"""Testes do caminho JSON (Opção 3) no llm_client — parsing e fallback (sem LLM)."""

from __future__ import annotations

from unittest.mock import MagicMock

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
    def test_strips_numeric_and_string_constraints(self):
        schema = {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "multipleOf": 1,
        }
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
        assert out["properties"]["score"] == {"type": "integer"}
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
        # minimum/maximum removidos no wire; additionalProperties preservado
        assert fmt["schema"]["properties"]["score"] == {"type": "integer"}
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
