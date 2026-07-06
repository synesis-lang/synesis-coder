"""Testes do caminho JSON (Opção 3) no llm_client — parsing e fallback (sem LLM)."""

from __future__ import annotations

from synesis_coder.llm_client import _parse_json_response


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
