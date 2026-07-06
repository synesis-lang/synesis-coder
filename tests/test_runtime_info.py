"""Testes do banner de runtime (runtime_info) — sem LLM real.

Cobre: presença das 4 informações (versão coder, versão compilador, backend/modelo,
caminho), escolha do rótulo de caminho conforme supports_json_schema(), dica
acionável só no backend anthropic em texto-livre, e emissão via logger.info.
"""

from __future__ import annotations

import logging

from synesis_coder import runtime_info
from synesis_coder.runtime_info import build_banner_line, runtime_banner


class _FakeClient:
    """Stub mínimo com a superfície que o banner consome."""

    def __init__(self, backend: str, model: str, json_schema: bool) -> None:
        self.backend = backend
        self.model = model
        self._json = json_schema

    def supports_json_schema(self) -> bool:
        return self._json


def test_banner_contains_all_four_infos():
    line = build_banner_line(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert "synesis-coder" in line
    assert "compilador synesis" in line
    assert "anthropic/claude-opus-4-6" in line
    assert "caminho:" in line


def test_label_free_text_when_no_json_schema():
    line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "texto-livre (regex)" in line
    assert "JSON assembler" not in line


def test_label_json_assembler_when_supported():
    line = build_banner_line(_FakeClient("openai", "gemma", True))
    assert "JSON assembler (determinístico)" in line
    assert "texto-livre" not in line


def test_hint_only_for_anthropic_free_text():
    anthropic_line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "SYNESIS_CODER_BACKEND=openai" in anthropic_line

    # openai com json ativo: sem dica
    openai_line = build_banner_line(_FakeClient("openai", "m", True))
    assert "SYNESIS_CODER_BACKEND=openai" not in openai_line


def test_no_hint_for_non_anthropic_without_json():
    # Backend openai-compat que (hipoteticamente) não suporta json_schema:
    # a dica de trocar para openai não se aplica.
    line = build_banner_line(_FakeClient("openai", "m", False))
    assert "SYNESIS_CODER_BACKEND=openai" not in line


def test_runtime_banner_emits_via_logger(caplog):
    with caplog.at_level(logging.INFO, logger=runtime_info.__name__):
        runtime_banner(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert any("caminho:" in rec.message for rec in caplog.records)
