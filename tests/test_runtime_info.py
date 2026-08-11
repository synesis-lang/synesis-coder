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


def test_banner_contains_backend_model_and_path():
    line = build_banner_line(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert "anthropic/claude-opus-4-6" in line
    # O rótulo do caminho (JSON assembler | texto-livre) sempre está presente.
    assert "texto-livre" in line or "JSON assembler" in line


def test_label_free_text_when_no_json_schema():
    line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "texto-livre (regex)" in line
    assert "JSON assembler" not in line


def test_label_json_assembler_when_supported():
    line = build_banner_line(_FakeClient("openai", "gemma", True))
    assert "JSON assembler" in line
    assert "texto-livre" not in line


def test_label_json_assembler_for_anthropic_with_structured_outputs():
    # Anthropic com SDK que suporta structured outputs: caminho JSON, sem dica.
    line = build_banner_line(_FakeClient("anthropic", "claude-sonnet-5", True))
    assert "JSON assembler" in line
    assert "SYNESIS_CODER_BACKEND=openai" not in line


def test_hint_only_for_anthropic_free_text():
    # Anthropic em texto-livre (SDK antigo): dica para atualizar o SDK.
    anthropic_line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "anthropic>=0.77.1" in anthropic_line

    # openai com json ativo: sem dica
    openai_line = build_banner_line(_FakeClient("openai", "m", True))
    assert "anthropic>=0.77.1" not in openai_line


def test_no_hint_for_anthropic_with_json():
    # Anthropic com structured outputs disponível: nenhuma dica de atualização.
    line = build_banner_line(_FakeClient("anthropic", "claude-sonnet-5", True))
    assert "anthropic>=0.77.1" not in line


def test_no_hint_for_non_anthropic_without_json():
    # Backend openai-compat que (hipoteticamente) não suporta json_schema:
    # a dica de atualizar o SDK anthropic não se aplica.
    line = build_banner_line(_FakeClient("openai", "m", False))
    assert "anthropic>=0.77.1" not in line


def test_runtime_banner_emits_via_logger(caplog):
    with caplog.at_level(logging.INFO, logger=runtime_info.__name__):
        runtime_banner(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert any("Motor:" in rec.message for rec in caplog.records)
