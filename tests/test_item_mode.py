"""Testes de integração para o modo item do synesis-coder.

Usa projetos reais de d:/GitHub/case-studies/ como fixtures.
Todos os testes que chamam o LLM requerem ANTHROPIC_API_KEY no ambiente.

Testes de project_loader e prompt_builder não chamam o LLM e podem
ser executados sem credenciais.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import synesis
from dotenv import load_dotenv

# Carregar .env antes de verificar a chave
load_dotenv()

# ---------------------------------------------------------------------------
# Caminhos dos projetos de teste (cases reais)
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")

PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"
PROJECT_AIDS = CASES_DIR / "Sociology/iramuteq_aids_corpus/aids_corpus.synp"
PROJECT_NAVE = CASES_DIR / "Theology/Nave_Topical_Concordance/nave.synp"
PROJECT_THOMPSON = CASES_DIR / "Theology/Thompson_Chain_Reference/thompson_bible.synp"

HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

requires_api_key = pytest.mark.skipif(
    not HAS_API_KEY, reason="ANTHROPIC_API_KEY não disponível"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_compiles(syn_output: str, ctx: dict) -> None:
    """Valida que syn_output compila sem erros estruturais via synesis.load().

    Usa a mesma lógica do validator: ignora OrphanItem (ITEM sem SOURCE),
    pois ao testar um ITEM isolado não carregamos todo o .syn do projeto.
    """
    from synesis_coder.validator import _has_structural_errors

    validation = synesis.load(
        project_content=ctx["project_content"],
        template_content=ctx["template_content"],
        annotation_contents={"test.syn": syn_output},
        bibliography_content=ctx.get("bib_content"),
    )
    assert not _has_structural_errors(validation), (
        f"Output não compilou:\n{validation.get_diagnostics()}\n\nOutput:\n{syn_output}"
    )


# ---------------------------------------------------------------------------
# Testes de project_loader (sem LLM)
# ---------------------------------------------------------------------------


class TestProjectLoader:
    """Testes para load_project() — sem chamadas ao LLM."""

    def test_load_social_acceptance(self):
        """Template completo: GUIDELINES, ORDERED, ENUMERATED, SCALE, CHAIN."""
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_SOCIAL)

        assert ctx["has_ontology_scope"] is True
        assert ctx["has_chain_field"] is True
        assert ctx["chain_field_name"] == "chain"
        assert len(ctx["chain_relations"]) > 0
        assert ctx["project_description"] is not None
        assert len(ctx["item_fields"]) > 0
        assert len(ctx["ontology_fields"]) > 0
        assert "text" in ctx["required_item"]

    def test_load_thompson_no_ontology_scope(self):
        """Template sem ONTOLOGY scope — has_ontology_scope deve ser False."""
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_THOMPSON)

        assert ctx["has_ontology_scope"] is False
        assert ctx["project_description"] is None  # thompson não tem DESCRIPTION

    def test_load_nave(self):
        """Template sem CHAIN field."""
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_NAVE)

        assert ctx["has_chain_field"] is False
        assert ctx["chain_relations"] == {}

    def test_load_aids_corpus(self):
        """Template com CHAIN, relações em português, sem GUIDELINES."""
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_AIDS)

        assert ctx["has_chain_field"] is True
        assert len(ctx["chain_relations"]) > 0

    def test_code_index_populated(self):
        """Projetos com .syn existente devem ter code_index populado."""
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_SOCIAL)
        # social_acceptance.syn existe no projeto
        assert not ctx["code_index"]["empty"]
        assert len(ctx["code_index"]["codes"]) > 0

    def test_project_not_found_raises(self):
        """Projeto inexistente deve levantar FileNotFoundError."""
        from synesis_coder.project_loader import load_project

        with pytest.raises(FileNotFoundError):
            load_project(Path("d:/nao_existe/projeto.synp"))


# ---------------------------------------------------------------------------
# Testes de prompt_builder (sem LLM)
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    """Testes para build_item_prompt() — sem chamadas ao LLM."""

    def test_prompt_structure(self):
        """Prompt deve ter mensagem system (cacheável) e user (dinâmica)."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_item_prompt(ctx, "smith2024", "Texto de teste.")

        assert len(messages) == 2
        system_msg = messages[0]
        user_msg = messages[1]

        assert system_msg["role"] == "system"
        assert system_msg["cache"] is True
        assert user_msg["role"] == "user"
        assert user_msg["cache"] is False

    def test_system_prompt_contains_project_description(self):
        """System prompt deve incluir descrição do projeto quando presente."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_item_prompt(ctx, "smith2024", "Texto.")

        assert ctx["project_description"] is not None
        assert ctx["project_description"][:30] in messages[0]["content"]

    def test_system_prompt_contains_field_instructions(self):
        """System prompt deve listar campos ITEM do template."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_item_prompt(ctx, "smith2024", "Texto.")
        system_content = messages[0]["content"]

        for field_name in ctx["item_fields"]:
            assert field_name in system_content

    def test_system_prompt_contains_chain_relations(self):
        """System prompt deve listar relações do campo CHAIN."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_item_prompt(ctx, "smith2024", "Texto.")
        system_content = messages[0]["content"]

        for rel_name in ctx["chain_relations"]:
            assert rel_name in system_content

    def test_user_message_contains_bibref_and_text(self):
        """Mensagem do usuário deve conter bibref e texto."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_THOMPSON)
        bibref = "genesis1"
        text = "In the beginning God created the heavens and the earth."
        messages = build_item_prompt(ctx, bibref, text)

        user_content = messages[1]["content"]
        assert bibref in user_content
        assert text in user_content

    def test_prompt_no_ontology_scope(self):
        """Projeto sem ONTOLOGY scope não deve adicionar seção de ontologia."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_item_prompt

        ctx = load_project(PROJECT_THOMPSON)
        assert ctx["has_ontology_scope"] is False
        # Deve funcionar normalmente mesmo sem ONTOLOGY scope
        messages = build_item_prompt(ctx, "genesis1", "In the beginning...")
        assert len(messages) == 2


# ---------------------------------------------------------------------------
# Testes de integração com LLM (requerem ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


class TestItemModeIntegration:
    """Testes de integração end-to-end com LLM real."""

    @requires_api_key
    def test_item_social_acceptance_compiles(self):
        """Output gerado para social_acceptance deve compilar sem erros."""
        from synesis_coder.modes.item_mode import process_item
        from synesis_coder.project_loader import load_project

        result = process_item(
            project_path=PROJECT_SOCIAL,
            bibref="ashworth2019",
            text=(
                "Community trust and environmental concern are the most important "
                "factors influencing social acceptance of wind energy projects. "
                "Local ownership models significantly reduce opposition."
            ),
            format="plain",
        )

        ctx = load_project(PROJECT_SOCIAL, load_annotations=False)
        _assert_compiles(result, ctx)

    @requires_api_key
    def test_item_thompson_no_ontology_scope(self):
        """Modo item deve funcionar normalmente em projeto sem ONTOLOGY scope."""
        from synesis_coder.modes.item_mode import process_item
        from synesis_coder.project_loader import load_project

        result = process_item(
            project_path=PROJECT_THOMPSON,
            bibref="genesis1",
            text="In the beginning God created the heavens and the earth.",
            format="plain",
        )

        ctx = load_project(PROJECT_THOMPSON, load_annotations=False)
        _assert_compiles(result, ctx)

    @requires_api_key
    def test_item_aids_corpus_compiles(self):
        """Output para aids_corpus (CHAIN em PT, sem GUIDELINES) deve compilar."""
        from synesis_coder.modes.item_mode import process_item
        from synesis_coder.project_loader import load_project

        result = process_item(
            project_path=PROJECT_AIDS,
            bibref="parker1994",
            text=(
                "A AIDS é percebida como uma doença do outro, associada a grupos "
                "marginalizados. O estigma social dificulta o acesso ao tratamento."
            ),
            format="plain",
        )

        ctx = load_project(PROJECT_AIDS, load_annotations=False)
        _assert_compiles(result, ctx)

    @requires_api_key
    def test_item_verbose_format(self):
        """Formato verbose deve incluir header com status."""
        from synesis_coder.modes.item_mode import process_item

        result = process_item(
            project_path=PROJECT_THOMPSON,
            bibref="genesis1",
            text="In the beginning God created the heavens and the earth.",
            format="verbose",
        )

        assert "# synesis-coder item" in result
        assert "# bibref: @genesis1" in result

    @requires_api_key
    def test_item_synesis_init_project(self):
        """Modo item deve funcionar com projeto gerado por 'synesis init'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # Criar projeto mínimo via synesis init
            proc = subprocess.run(
                ["synesis", "init"],
                capture_output=True,
                text=True,
                cwd=str(tmp_path),
            )
            if proc.returncode != 0:
                pytest.skip(f"synesis init falhou: {proc.stderr}")

            # Localizar o .synp gerado
            synp_files = list(tmp_path.glob("*.synp"))
            if not synp_files:
                pytest.skip("synesis init não gerou arquivo .synp")

            from synesis_coder.modes.item_mode import process_item
            from synesis_coder.project_loader import load_project

            result = process_item(
                project_path=synp_files[0],
                bibref="smith2024",
                text="Social cohesion enables collective action in resilient communities.",
                format="plain",
            )

            ctx = load_project(synp_files[0], load_annotations=False)
            _assert_compiles(result, ctx)
