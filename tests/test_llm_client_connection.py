"""Testes para a conexão explícita do LLMClient e a conexão de crítica.

Cobrem as peças (a) e (b) do Estudo_API_por_Fase:
- api_url/api_key como parâmetros opcionais (override vs fallback ao ambiente);
- get_critique_connection() resolvendo a 2ª conexão (CRITIQUE_*) com herança.

Nenhum teste faz chamada de rede — os clients openai/anthropic são construídos
(sem validar a chave) e apenas seus atributos base_url/api_key são inspecionados.
"""

from __future__ import annotations

import pytest

from synesis_coder.llm_client import LLMClient, get_critique_connection


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove todas as vars de conexão para isolar cada teste."""
    for var in (
        "SYNESIS_CODER_BACKEND",
        "SYNESIS_CODER_API_URL",
        "SYNESIS_CODER_API_KEY",
        "ANTHROPIC_API_KEY",
        "SYNESIS_CODER_CRITIQUE_BACKEND",
        "SYNESIS_CODER_CRITIQUE_API_URL",
        "SYNESIS_CODER_CRITIQUE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Peça (a): api_url / api_key como parâmetros
# ---------------------------------------------------------------------------


class TestExplicitConnection:
    def test_openai_api_url_and_key_override_env(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://primary.example/api")
        monkeypatch.setenv("SYNESIS_CODER_API_KEY", "primary-key")

        c = LLMClient(
            model="m", backend="openai",
            api_url="https://second.example/api", api_key="second-key",
        )
        assert str(c._client.base_url).startswith("https://second.example/api")
        assert c._client.api_key == "second-key"

    def test_openai_falls_back_to_env_when_none(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://primary.example/api")
        monkeypatch.setenv("SYNESIS_CODER_API_KEY", "primary-key")

        c = LLMClient(model="m", backend="openai")
        assert str(c._client.base_url).startswith("https://primary.example/api")
        assert c._client.api_key == "primary-key"

    def test_anthropic_api_key_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic")
        c = LLMClient(model="m", backend="anthropic", api_key="explicit-anthropic")
        assert c._client.api_key == "explicit-anthropic"

    def test_anthropic_falls_back_to_env_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic")
        c = LLMClient(model="m", backend="anthropic")
        assert c._client.api_key == "env-anthropic"


# ---------------------------------------------------------------------------
# Peça (b): get_critique_connection
# ---------------------------------------------------------------------------


class TestGetCritiqueConnection:
    def test_all_none_when_no_critique_vars(self):
        """Sem vars CRITIQUE_* → dict com None (herda a conexão global)."""
        conn = get_critique_connection()
        assert conn == {"backend": None, "api_url": None, "api_key": None}

    def test_reads_critique_vars_when_present(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_BACKEND", "anthropic")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_API_KEY", "crit-key")
        conn = get_critique_connection()
        assert conn["backend"] == "anthropic"
        assert conn["api_key"] == "crit-key"
        assert conn["api_url"] is None  # não definido → None

    def test_empty_string_treated_as_none(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_BACKEND", "")
        conn = get_critique_connection()
        assert conn["backend"] is None


# ---------------------------------------------------------------------------
# Integração: critique connection distinta da primária
# ---------------------------------------------------------------------------


class TestCritiqueConnDistinctFromPrimary:
    def test_critic_anthropic_while_primary_openai(self, monkeypatch):
        """Primário openai; crítica anthropic com chave própria — conexões distintas."""
        # Primária: OpenRouter (openai-compat)
        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "openai")
        monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://openrouter.example/api")
        monkeypatch.setenv("SYNESIS_CODER_API_KEY", "or-key")
        # Crítica: Anthropic nativa
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_BACKEND", "anthropic")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_API_KEY", "ant-key")

        # Gerador (primária)
        gen = LLMClient(model="deepseek/deepseek-v4-pro")
        assert gen.backend == "openai"
        assert str(gen._client.base_url).startswith("https://openrouter.example/api")

        # Crítico (2ª conexão)
        conn = get_critique_connection()
        crit = LLMClient(model="claude-sonnet-4-6", **conn)
        assert crit.backend == "anthropic"
        assert crit._client.api_key == "ant-key"

    def test_critic_inherits_primary_when_no_critique_conn(self, monkeypatch):
        """Sem CRITIQUE_* de conexão → crítico usa a conexão primária (retrocompat)."""
        monkeypatch.setenv("SYNESIS_CODER_BACKEND", "openai")
        monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://primary.example/api")
        monkeypatch.setenv("SYNESIS_CODER_API_KEY", "primary-key")

        crit = LLMClient(model="m", **get_critique_connection())
        assert crit.backend == "openai"
        assert str(crit._client.base_url).startswith("https://primary.example/api")
        assert crit._client.api_key == "primary-key"


# ---------------------------------------------------------------------------
# Peças (c)/(d): os modos passam a conexão de crítica ao instanciar o client
# ---------------------------------------------------------------------------


class TestModesUseCritiqueConnection:
    def test_critique_mode_passes_critique_conn(self, monkeypatch, tmp_path):
        """process_critique instancia o LLMClient com a conexão de crítica."""
        import textwrap
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from synesis_coder.modes.critique_mode import process_critique

        project = Path("d:/GitHub/case-studies/Sociology/Social_Acceptance/social_acceptance.synp")
        if not project.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_BACKEND", "anthropic")
        monkeypatch.setenv("SYNESIS_CODER_CRITIQUE_API_KEY", "crit-key")

        syn = tmp_path / "c.syn"
        syn.write_text(textwrap.dedent("""\
            ITEM @smith2024
                text: t
                chain: A -> ENABLES -> B
            END ITEM
        """), encoding="utf-8")

        captured = {}

        def _fake_ctor(*args, **kwargs):
            captured.update(kwargs)
            m = MagicMock()
            m.model = kwargs.get("model") or "m"
            m.backend = kwargs.get("backend") or "openai"
            m.usage.summary_line.return_value = "tokens: 0"
            m.call_async = AsyncMock(return_value="# $suspicion_score: 0.0\n# $reason: none")
            return m

        with patch("synesis_coder.modes.critique_mode.LLMClient", side_effect=_fake_ctor):
            process_critique(syn_path=syn, project_path=project, format="plain")

        # Os kwargs de conexão de crítica chegaram ao construtor.
        assert captured.get("backend") == "anthropic"
        assert captured.get("api_key") == "crit-key"

    def test_refine_critic_uses_critique_conn_generator_primary(self, monkeypatch):
        """No refine, o crítico recebe a conexão de crítica; o gerador, a primária."""
        # Verifica a fiação: critique_client leva **conn; refine_client não.
        import inspect

        from synesis_coder.modes import refine_mode
        src = inspect.getsource(refine_mode._process_refine_async)
        assert "get_critique_connection()" in src
        # gerador não deve receber a conexão de crítica
        assert "refine_client = LLMClient(model=refine_model)" in src
