"""Testes unitários para _validate_phase_env() em cli.py.

Todos os testes são unitários — sem LLM, sem I/O de rede.
Usam monkeypatch para isolar variáveis de ambiente.
"""

from __future__ import annotations

import pytest

from synesis_coder.cli import _validate_phase_env

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove variáveis de ambiente do coder para cada teste começar limpo.

    Neutraliza load_dotenv() para que o arquivo .env local não reponha as
    variáveis deletadas pelo monkeypatch durante o teste.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    for var in (
        "ANTHROPIC_API_KEY",
        "SYNESIS_CODER_BACKEND",
        "SYNESIS_CODER_MODEL",
        "SYNESIS_CODER_CRITIQUE_MODEL",
        "SYNESIS_CODER_NORMALIZATION_MODEL",
        "SYNESIS_CODER_INCORPORATION_MODEL",
        "SYNESIS_CODER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Backend Anthropic — comportamento padrão
# ---------------------------------------------------------------------------


class TestAnthropicBackend:
    def test_missing_api_key_raises(self, monkeypatch):
        """Sem ANTHROPIC_API_KEY deve levantar EnvironmentError."""
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "claude-opus-4-6")
        with pytest.raises(EnvironmentError):
            _validate_phase_env("critique")

    def test_with_api_key_returns_model(self, monkeypatch):
        """Com API key configurada, retorna modelo sem erro."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "claude-opus-4-6")
        result = _validate_phase_env("critique")
        assert result == "claude-opus-4-6"

    def test_error_message_contains_api_key_var(self, monkeypatch):
        """Mensagem de erro menciona ANTHROPIC_API_KEY."""
        with pytest.raises(EnvironmentError) as exc_info:
            _validate_phase_env("critique")
        assert "ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_error_message_contains_phase_model_var(self, monkeypatch):
        """Mensagem de erro menciona a variável específica da fase."""
        with pytest.raises(EnvironmentError) as exc_info:
            _validate_phase_env("critique")
        assert "SYNESIS_CODER_CRITIQUE_MODEL" in str(exc_info.value)

    def test_error_message_contains_example_model(self, monkeypatch):
        """Mensagem de erro inclui exemplo de modelo recomendado."""
        with pytest.raises(EnvironmentError) as exc_info:
            _validate_phase_env("critique")
        # Deve ter exemplo de modelo válido (sonnet ou opus)
        msg = str(exc_info.value)
        assert "claude-sonnet-4-6" in msg or "claude-opus-4-6" in msg

    def test_error_message_mentions_phase_name(self, monkeypatch):
        """Mensagem de erro menciona o nome da fase para contexto."""
        with pytest.raises(EnvironmentError) as exc_info:
            _validate_phase_env("normalization")
        assert "normalization" in str(exc_info.value)

    def test_explicit_backend_anthropic(self, monkeypatch):
        """Backend explicitamente 'anthropic' valida API key normalmente."""
        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _validate_phase_env("critique")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Backend OpenAI — sem requisito de API key
# ---------------------------------------------------------------------------


class TestOpenAIBackend:
    def test_openai_backend_no_key_required(self, monkeypatch):
        """Backend openai não exige ANTHROPIC_API_KEY."""
        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "openai")
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "llama3.2")
        # Não deve levantar mesmo sem ANTHROPIC_API_KEY
        result = _validate_phase_env("critique")
        assert result == "llama3.2"

    def test_openai_backend_returns_phase_model(self, monkeypatch):
        """Backend openai usa modelo específico da fase."""
        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "openai")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_MODEL", "gemma4:e2b")
        result = _validate_phase_env("critique")
        assert result == "gemma4:e2b"


# ---------------------------------------------------------------------------
# Resolução de modelo — precedência e fallback
# ---------------------------------------------------------------------------


class TestModelResolution:
    def test_phase_specific_model_takes_precedence(self, monkeypatch):
        """SYNESIS_CODER_<PHASE>_MODEL tem precedência sobre SYNESIS_CODER_MODEL."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_MODEL", "claude-sonnet-4-6")
        result = _validate_phase_env("critique")
        assert result == "claude-sonnet-4-6"

    def test_fallback_to_synesis_coder_model(self, monkeypatch):
        """Sem variável de fase, usa SYNESIS_CODER_MODEL."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("SYNESIS_CODER_MODEL", "claude-haiku-4-5-20251001")
        result = _validate_phase_env("critique")
        assert result == "claude-haiku-4-5-20251001"

    def test_fallback_to_hardcoded_default(self, monkeypatch):
        """Sem nenhuma variável de modelo, retorna o padrão hardcoded."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _validate_phase_env("critique")
        assert result == "claude-opus-4-6"

    def test_each_phase_resolves_independently(self, monkeypatch):
        """Fases diferentes resolvem modelos independentemente."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_MODEL", "claude-sonnet-4-6")
        monkeypatch.setenv("SYNESIS_CODER_NORMALIZATION_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("SYNESIS_CODER_INCORPORATION_MODEL", "claude-haiku-4-5-20251001")

        assert _validate_phase_env("critique") == "claude-sonnet-4-6"
        assert _validate_phase_env("normalization") == "claude-opus-4-6"
        assert _validate_phase_env("incorporation") == "claude-haiku-4-5-20251001"

    def test_phase_name_case_insensitive_env_var(self, monkeypatch):
        """Variável de ambiente é construída em uppercase independente do case do input."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_MODEL", "claude-sonnet-4-6")
        # Passando em lowercase como esperado
        result = _validate_phase_env("critique")
        assert result == "claude-sonnet-4-6"

    def test_incorporation_phase_resolves_model(self, monkeypatch):
        """Fase incorporation (determinística) também resolve modelo corretamente."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("SYNESIS_CODER_INCORPORATION_MODEL", "claude-sonnet-4-6")
        result = _validate_phase_env("incorporation")
        assert result == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Retorno de tipo e consistência
# ---------------------------------------------------------------------------


class TestReturnContract:
    def test_returns_string(self, monkeypatch):
        """Sempre retorna uma string não-vazia quando bem-sucedido."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _validate_phase_env("critique")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_all_pipeline_phases_succeed(self, monkeypatch):
        """Todas as 3 fases do pipeline validam sem erro com config mínima."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        for phase in ("critique", "normalization", "incorporation"):
            result = _validate_phase_env(phase)
            assert isinstance(result, str) and len(result) > 0
