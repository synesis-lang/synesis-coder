"""Testes para o reenvio do system prompt (GUIDELINES) no loop de correção.

Bug corrigido: fix()/fix_async() montavam a chamada de correção SEM nenhuma
mensagem `system`, de modo que o LLM corrigia sem as GUIDELINES do template
(réguas de score, proibições de domínio, code_index). Como o laço de
validate_and_fix reatribui `output` a cada iteração, o efeito era cumulativo:
cada rodada corrigia um artefato já produzido sem as regras, também sem as
regras.

Ver Planning/Estudo_Fix_Perde_System_Prompt.md.

Todos os testes são offline — sem LLM, sem IO de rede.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synesis_coder.llm_client import LLMClient

# Fixture real: o system prompt só é representativo com um template de verdade
# (GUIDELINES, relações de chain, code_index). Um ctx sintético não exercita
# o caminho que o bug afetava.
_PROJECT = Path("d:/GitHub/case-studies/Sociology/Social_Acceptance/social_acceptance.synp")


def _load_ctx() -> dict:
    from synesis_coder.project_loader import load_project

    return load_project(_PROJECT)


def _make_client(backend: str = "anthropic") -> LLMClient:
    """LLMClient com cliente interno mockado (sem API key real)."""
    if backend == "anthropic":
        with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="k"):
            client = LLMClient(model="claude-sonnet-4-6", backend="anthropic")
    else:
        with patch("synesis_coder.llm_client._get_api_url", return_value="http://x"), \
             patch("synesis_coder.llm_client._get_api_key", return_value="k"):
            client = LLMClient(model="gpt-5.6-luna", backend="openai")
    client._client = MagicMock()
    client._rate_limit_enabled = False
    return client


def _anthropic_response(text: str = "ITEM @r\nEND ITEM") -> MagicMock:
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.usage = MagicMock(input_tokens=10, output_tokens=5)
    response.stop_reason = "end_turn"
    return response


GUIDELINES = "You are a coder.\nRULE: never emit criterio without evidence."


class TestFixMessageAssembly:
    """_build_fix_messages: estrutura das mensagens de correção."""

    def test_without_system_has_only_user(self):
        """Sem system, comportamento antigo preservado (uma mensagem user)."""
        msgs = LLMClient._build_fix_messages("bad", "E017")

        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_with_system_puts_it_first_and_caches_it(self):
        """Com system, ele vem PRIMEIRO e marcado para cache."""
        msgs = LLMClient._build_fix_messages("bad", "E017", GUIDELINES)

        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == GUIDELINES
        assert msgs[0]["cache"] is True, "system deve ser cacheável no fix"
        assert msgs[1]["role"] == "user"

    def test_user_message_still_carries_output_and_errors(self):
        """A mensagem user mantém o bloco anterior e os diagnósticos."""
        msgs = LLMClient._build_fix_messages("BAD BLOCK", "E017 mismatch", GUIDELINES)
        user = msgs[-1]["content"]

        assert "BAD BLOCK" in user
        assert "E017 mismatch" in user


class TestFixSendsSystemToAPI:
    """O system prompt chega efetivamente ao payload da API."""

    def test_anthropic_fix_includes_system_kwarg(self):
        """Branch Anthropic: kwargs['system'] presente e com as GUIDELINES."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        client.fix("bad", "E017", system=GUIDELINES)

        kwargs = client._client.messages.create.call_args.kwargs
        assert "system" in kwargs, "fix() deve enviar system para a API"
        assert GUIDELINES in kwargs["system"][0]["text"]

    def test_anthropic_fix_marks_system_for_caching(self):
        """O bloco system leva cache_control — habilita o cache read."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        client.fix("bad", "E017", system=GUIDELINES)

        block = client._client.messages.create.call_args.kwargs["system"][0]
        assert block.get("cache_control") == {"type": "ephemeral"}

    def test_anthropic_fix_without_system_omits_kwarg(self):
        """Sem system, o kwarg não é enviado (comportamento antigo)."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        client.fix("bad", "E017")

        assert "system" not in client._client.messages.create.call_args.kwargs

    def test_fix_async_includes_system(self):
        """fix_async() também repassa o system."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        asyncio.run(client.fix_async("bad", "E017", system=GUIDELINES))

        kwargs = client._client.messages.create.call_args.kwargs
        assert GUIDELINES in kwargs["system"][0]["text"]

    def test_fix_still_marks_correction(self):
        """A contabilidade de correções não regride com a mudança."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        client.fix("bad", "E017", system=GUIDELINES)

        assert client.usage.corrections == 1


class TestFixPropagatesSchema:
    """O JSON Schema também deixa de ser descartado no fix."""

    def test_anthropic_fix_sends_output_config(self):
        """Com schema, o fix mantém structured outputs em vez de texto livre."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response('{"items":[]}')
        schema = {"type": "object", "properties": {}, "additionalProperties": False}

        with patch.object(client, "supports_json_schema", return_value=True):
            client.fix("bad", "E017", system=GUIDELINES, schema=schema)

        kwargs = client._client.messages.create.call_args.kwargs
        assert "output_config" in kwargs, "schema deve chegar ao wire no fix"

    def test_schema_ignored_when_backend_unsupported(self):
        """Backend sem suporte a schema não recebe output_config (sem 400)."""
        client = _make_client("anthropic")
        client._client.messages.create.return_value = _anthropic_response()

        with patch.object(client, "supports_json_schema", return_value=False):
            client.fix("bad", "E017", schema={"type": "object"})

        assert "output_config" not in client._client.messages.create.call_args.kwargs


class TestValidatorPassesSystem:
    """O validator reconstrói e repassa o system prompt."""

    @pytest.mark.skipif(not _PROJECT.exists(), reason="fixture de case-study ausente")
    def test_fix_system_prompt_item_scope(self):
        """scope='item' devolve o system prompt de ITEM, com as GUIDELINES."""
        from synesis_coder.validator import _fix_system_prompt

        result = _fix_system_prompt(_load_ctx(), "item")

        assert result is not None
        assert "ITEM" in result
        # O ponto do bug: as instruções de campo do template têm que estar lá.
        assert len(result) > 500, "system prompt suspeito de estar vazio"

    @pytest.mark.skipif(not _PROJECT.exists(), reason="fixture de case-study ausente")
    def test_fix_system_prompt_abstract_scope(self):
        """scope='abstract' devolve o system prompt de SOURCE + ITEMs."""
        from synesis_coder.validator import _fix_system_prompt

        result = _fix_system_prompt(_load_ctx(), "abstract")

        assert result is not None
        assert "SOURCE" in result

    @pytest.mark.skipif(not _PROJECT.exists(), reason="fixture de case-study ausente")
    def test_scopes_produce_different_prompts(self):
        """Os dois escopos não são intercambiáveis — abstract cobre SOURCE."""
        from synesis_coder.validator import _fix_system_prompt

        ctx = _load_ctx()
        assert _fix_system_prompt(ctx, "item") != _fix_system_prompt(ctx, "abstract")

    def test_fix_system_prompt_degrades_gracefully(self):
        """ctx inválido não derruba a validação — devolve None."""
        from synesis_coder.validator import _fix_system_prompt

        assert _fix_system_prompt({}, "abstract") is None

    def test_validate_and_fix_forwards_system(self):
        """validate_and_fix repassa o system reconstruído ao fix()."""
        from synesis_coder import validator

        client = MagicMock()
        client.fix.return_value = "ITEM @r\nEND ITEM"
        ctx = {"project_content": "P", "template_content": "T"}

        with patch.object(validator, "synesis") as mock_syn, \
             patch.object(validator, "_fix_system_prompt", return_value=GUIDELINES), \
             patch.object(validator, "_has_structural_errors", return_value=True):
            mock_syn.load.return_value = MagicMock(
                get_diagnostics=MagicMock(return_value="E017")
            )
            validator.validate_and_fix("bad", ctx, client, max_tries=1)

        assert client.fix.called
        assert client.fix.call_args.kwargs.get("system") == GUIDELINES

    def test_validate_and_fix_async_forwards_system(self):
        """validate_and_fix_async idem, no caminho de lote."""
        from synesis_coder import validator

        client = MagicMock()

        async def _fake_fix(*a, **kw):
            return "SOURCE @r\nEND SOURCE"

        client.fix_async = MagicMock(side_effect=_fake_fix)
        ctx = {"project_content": "P", "template_content": "T"}

        with patch.object(validator, "synesis") as mock_syn, \
             patch.object(validator, "_fix_system_prompt", return_value=GUIDELINES), \
             patch.object(validator, "_has_structural_errors", return_value=True):
            mock_syn.load.return_value = MagicMock(
                get_diagnostics=MagicMock(return_value="E017")
            )
            asyncio.run(
                validator.validate_and_fix_async("bad", ctx, client, max_tries=1)
            )

        assert client.fix_async.called
        assert client.fix_async.call_args.kwargs.get("system") == GUIDELINES
