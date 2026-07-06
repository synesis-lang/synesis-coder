"""Testes para o modo abstract do synesis-coder.

Usa projetos reais de d:/GitHub/case-studies/ como fixtures.

Testes de parse_bib_entries e build_abstract_prompt não chamam o LLM.
Testes de integração requerem ANTHROPIC_API_KEY no ambiente.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import synesis
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Caminhos dos projetos e .bib de teste
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")

PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"
BIB_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.bib"

PROJECT_AIDS = CASES_DIR / "Sociology/iramuteq_aids_corpus/aids_corpus.synp"

PROJECT_THOMPSON = CASES_DIR / "Theology/Thompson_Chain_Reference/thompson_bible.synp"

HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

requires_api_key = pytest.mark.skipif(
    not HAS_API_KEY, reason="ANTHROPIC_API_KEY não disponível"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_compiles_annotation(syn_output: str, ctx: dict) -> None:
    """Valida que syn_output (SOURCE + ITEMs) compila sem erros estruturais."""
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


def _create_test_bib(tmpdir: Path, entries: list[dict]) -> Path:
    """Cria um .bib temporário para testes."""
    bib_path = tmpdir / "test.bib"
    lines = []
    for entry in entries:
        bibref = entry["bibref"]
        abstract = entry.get("abstract", "")
        title = entry.get("title", f"Title for {bibref}")
        lines.append(f"@article{{{bibref},")
        lines.append(f"  title = {{{title}}},")
        lines.append(f"  author = {{Author, A.}},")
        lines.append(f"  year = {{2024}},")
        if abstract:
            lines.append(f"  abstract = {{{abstract}}},")
        lines.append("}")
        lines.append("")
    bib_path.write_text("\n".join(lines), encoding="utf-8")
    return bib_path


# ---------------------------------------------------------------------------
# Testes de parse_bib_entries (sem LLM)
# ---------------------------------------------------------------------------


class TestParseBibEntries:
    """Testes para parse_bib_entries() — sem chamadas ao LLM."""

    def test_parse_real_bib(self):
        """Deve carregar entradas com abstract do .bib real."""
        from synesis_coder.modes.abstract_mode import parse_bib_entries

        if not BIB_SOCIAL.exists():
            pytest.skip(f"Arquivo .bib não encontrado: {BIB_SOCIAL}")

        entries = parse_bib_entries(BIB_SOCIAL)
        assert len(entries) > 0
        for entry in entries:
            assert "bibref" in entry
            assert "abstract" in entry
            assert len(entry["bibref"]) > 0
            assert len(entry["abstract"]) > 0

    def test_parse_bib_skips_entries_without_abstract(self):
        """Entradas sem abstract devem ser ignoradas."""
        from synesis_coder.modes.abstract_mode import parse_bib_entries

        with tempfile.TemporaryDirectory() as tmpdir:
            bib_path = _create_test_bib(Path(tmpdir), [
                {"bibref": "with_abstract", "abstract": "This is a test abstract."},
                {"bibref": "without_abstract"},  # sem abstract
            ])
            entries = parse_bib_entries(bib_path)
            assert len(entries) == 1
            assert entries[0]["bibref"] == "with_abstract"

    def test_parse_bib_raises_on_no_abstracts(self):
        """Deve levantar ValueError se nenhuma entrada tem abstract."""
        from synesis_coder.modes.abstract_mode import parse_bib_entries

        with tempfile.TemporaryDirectory() as tmpdir:
            bib_path = _create_test_bib(Path(tmpdir), [
                {"bibref": "no_abstract_1"},
                {"bibref": "no_abstract_2"},
            ])
            with pytest.raises(ValueError, match="Nenhuma entrada"):
                parse_bib_entries(bib_path)

    def test_parse_bib_file_not_found(self):
        """Deve levantar FileNotFoundError para arquivo inexistente."""
        from synesis_coder.modes.abstract_mode import parse_bib_entries

        with pytest.raises(FileNotFoundError):
            parse_bib_entries(Path("d:/nao_existe/corpus.bib"))


# ---------------------------------------------------------------------------
# Testes de build_abstract_prompt (sem LLM)
# ---------------------------------------------------------------------------


class TestAbstractPromptBuilder:
    """Testes para build_abstract_prompt() — sem chamadas ao LLM."""

    def test_prompt_structure(self):
        """Prompt deve ter system (cacheável) + user (dinâmico)."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_prompt(ctx, "smith2024", "Test abstract.")

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["cache"] is True
        assert messages[1]["role"] == "user"
        assert messages[1]["cache"] is False

    def test_system_prompt_mentions_source_and_item(self):
        """System prompt deve instruir geração de SOURCE e ITEM."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_prompt(ctx, "smith2024", "Test abstract.")
        system_content = messages[0]["content"]

        assert "SOURCE" in system_content
        assert "ITEM" in system_content

    def test_system_prompt_contains_source_fields(self):
        """System prompt deve listar campos SOURCE do template."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_prompt(ctx, "smith2024", "Test abstract.")
        system_content = messages[0]["content"]

        for field_name in ctx["source_fields"]:
            assert field_name in system_content

    def test_system_prompt_contains_item_fields(self):
        """System prompt deve listar campos ITEM do template."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_prompt(ctx, "smith2024", "Test abstract.")
        system_content = messages[0]["content"]

        for field_name in ctx["item_fields"]:
            assert field_name in system_content

    def test_user_message_contains_bibref_and_abstract(self):
        """Mensagem do usuário deve conter bibref e abstract."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        bibref = "smith2024"
        abstract = "Community trust influences social acceptance."
        messages = build_abstract_prompt(ctx, bibref, abstract)

        user_content = messages[1]["content"]
        assert bibref in user_content
        assert abstract in user_content

    def test_prompt_includes_code_index(self):
        """System prompt deve incluir conceitos existentes quando disponíveis."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt

        ctx = load_project(PROJECT_SOCIAL)
        assert not ctx["code_index"]["empty"]

        messages = build_abstract_prompt(ctx, "smith2024", "Test abstract.")
        system_content = messages[0]["content"]

        assert "EXISTING PROJECT CONCEPTS" in system_content


# ---------------------------------------------------------------------------
# Testes de _extract_annotation_blocks (sem LLM)
# ---------------------------------------------------------------------------


class TestExtractAnnotationBlocks:
    """Testes para _extract_annotation_blocks()."""

    def test_extracts_source_and_items(self):
        """Deve extrair blocos SOURCE e ITEM, ignorando o resto."""
        from synesis_coder.validator import _extract_annotation_blocks

        text = """Aqui vai uma explicação...

SOURCE @smith2024
    description: Test
END SOURCE

Texto entre blocos...

ITEM @smith2024
    text: Test item
END ITEM

ONTOLOGY Community_Trust
    topic: Trust
END ONTOLOGY
"""
        result = _extract_annotation_blocks(text)
        assert "SOURCE @smith2024" in result
        assert "ITEM @smith2024" in result
        assert "ONTOLOGY" not in result

    def test_returns_empty_on_no_blocks(self):
        """Deve retornar string vazia se não encontrar blocos."""
        from synesis_coder.validator import _extract_annotation_blocks

        assert _extract_annotation_blocks("nenhum bloco aqui") == ""

    def test_handles_multiple_items(self):
        """Deve extrair múltiplos blocos ITEM."""
        from synesis_coder.validator import _extract_annotation_blocks

        text = """SOURCE @ref1
    description: Test
END SOURCE

ITEM @ref1
    text: First item
END ITEM

ITEM @ref1
    text: Second item
END ITEM
"""
        result = _extract_annotation_blocks(text)
        assert result.count("ITEM @ref1") == 2
        assert "SOURCE @ref1" in result


# ---------------------------------------------------------------------------
# Testes do caminho JSON (sem LLM)
# ---------------------------------------------------------------------------


class TestAbstractSchema:
    """Testes para build_abstract_schema — sem chamadas ao LLM."""

    def test_envelope_has_source_and_items(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.schema_builder import build_abstract_schema

        ctx = load_project(PROJECT_SOCIAL)
        schema = build_abstract_schema(ctx)

        assert schema["type"] == "object"
        assert "source" in schema["properties"]
        assert "items" in schema["properties"]
        assert schema["required"] == ["source", "items"]
        assert schema["additionalProperties"] is False

    def test_source_fields_match_template(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.schema_builder import build_abstract_schema

        ctx = load_project(PROJECT_SOCIAL)
        schema = build_abstract_schema(ctx)

        source_props = schema["properties"]["source"]["properties"]
        assert set(source_props) == set(ctx["source_fields"])

    def test_items_array_with_item_fields(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.schema_builder import build_abstract_schema

        ctx = load_project(PROJECT_SOCIAL)
        schema = build_abstract_schema(ctx)

        items_schema = schema["properties"]["items"]
        assert items_schema["type"] == "array"
        assert items_schema["minItems"] == 1
        item_props = items_schema["items"]["properties"]
        assert set(item_props) == set(ctx["item_fields"])

    def test_additional_properties_false_everywhere(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.schema_builder import build_abstract_schema

        ctx = load_project(PROJECT_SOCIAL)
        schema = build_abstract_schema(ctx)

        assert schema["additionalProperties"] is False
        assert schema["properties"]["source"]["additionalProperties"] is False
        assert schema["properties"]["items"]["items"]["additionalProperties"] is False


class TestAbstractValuesPrompt:
    """Testes para build_abstract_values_prompt — sem chamadas ao LLM."""

    def test_prompt_structure(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_values_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_values_prompt(ctx, "smith2024", "Abstract text.")

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["cache"] is True
        assert messages[1]["role"] == "user"
        assert messages[1]["cache"] is False

    def test_system_prompt_output_contract(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_values_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_values_prompt(ctx, "smith2024", "Abstract text.")
        system = messages[0]["content"]

        assert "OUTPUT CONTRACT" in system
        assert '"source"' in system
        assert '"items"' in system
        # Não deve instruir moldura de bloco
        assert "SOURCE @{bibref}" not in system
        assert "END SOURCE" not in system

    def test_user_message_contains_bibref_and_abstract(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_values_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_values_prompt(ctx, "abdin2024", "Test abstract.")
        user = messages[1]["content"]

        assert "@abdin2024" in user
        assert "Test abstract." in user

    def test_system_prompt_contains_all_item_fields(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_values_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_values_prompt(ctx, "smith2024", "Abstract text.")
        system = messages[0]["content"]

        for field_name in ctx["item_fields"]:
            assert field_name in system

    def test_system_prompt_contains_source_fields(self):
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_values_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_abstract_values_prompt(ctx, "smith2024", "Abstract text.")
        system = messages[0]["content"]

        for field_name in ctx["source_fields"]:
            assert field_name in system


class TestAssembleAbstractFromData:
    """Testa o assembler com dados sintéticos no formato do caminho JSON abstract."""

    def test_source_plus_items_assembled(self):
        from synesis_coder.block_assembler import assemble_items, assemble_source
        from synesis_coder.project_loader import load_project

        ctx = load_project(PROJECT_SOCIAL)
        source_data = {"description": "A study on trust.", "epistemic_model": "x", "method": "y"}
        items_data = {"items": [{
            "text": "Community trust enables social acceptance.",
            "note": "Key causal mechanism identified.",
            "chain": [{"source": "community_trust", "relation": "ENABLES", "target": "social_acceptance"}],
        }]}

        source_block = assemble_source(ctx, "abdin2024", source_data)
        items_block = assemble_items(ctx, "abdin2024", items_data)
        combined = source_block + "\n\n" + items_block

        assert "SOURCE @abdin2024" in combined
        assert "END SOURCE" in combined
        assert "ITEM @abdin2024" in combined
        assert "community_trust -> ENABLES -> social_acceptance" in combined
        assert "END ITEM" in combined

    def test_combined_output_compiles(self):
        from synesis_coder.block_assembler import assemble_items, assemble_source
        from synesis_coder.project_loader import load_project
        from synesis_coder.validator import _has_structural_errors

        ctx = load_project(PROJECT_SOCIAL)
        source_block = assemble_source(
            ctx, "abdin2024",
            {"description": "A study.", "epistemic_model": "x", "method": "y"},
        )
        items_block = assemble_items(
            ctx, "abdin2024",
            {"items": [{
                "text": "Trust enables acceptance.",
                "note": "Key finding.",
                "chain": [{"source": "trust", "relation": "ENABLES", "target": "acceptance"}],
            }]},
        )
        combined = source_block + "\n\n" + items_block

        validation = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"t.syn": combined},
            bibliography_content=ctx.get("bib_content"),
        )
        assert not _has_structural_errors(validation), validation.get_diagnostics()


# ---------------------------------------------------------------------------
# Testes de integração com LLM (requerem ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


class TestAbstractModeIntegration:
    """Testes de integração end-to-end com LLM real."""

    @requires_api_key
    def test_single_abstract_compiles(self):
        """Output para um abstract do social_acceptance deve compilar."""
        import asyncio
        from synesis_coder.llm_client import LLMClient
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_abstract_prompt
        from synesis_coder.validator import validate_and_fix_async

        ctx = load_project(PROJECT_SOCIAL)
        bibref = "ashworth2019"
        abstract = (
            "This paper examines public attitudes toward CCS technology "
            "in Australia and China. Results found that male respondents, "
            "those with higher perceived knowledge of CCS, and those who "
            "valued economic outcomes over environmental protection were "
            "more likely to support CCS."
        )

        client = LLMClient()
        messages = build_abstract_prompt(ctx, bibref, abstract)

        async def _run():
            raw = await client.call_async(messages, temperature=0.0)
            final, success = await validate_and_fix_async(
                raw, ctx, client, annotation_key=f"{bibref}.syn",
            )
            return final, success

        final_syn, success = asyncio.run(_run())

        assert success, f"Validação falhou:\n{final_syn}"
        assert "SOURCE @ashworth2019" in final_syn
        assert "ITEM @ashworth2019" in final_syn

        # Compilação final
        ctx_clean = load_project(PROJECT_SOCIAL, load_annotations=False)
        _assert_compiles_annotation(final_syn, ctx_clean)

    @requires_api_key
    def test_process_abstract_batch(self):
        """process_abstract deve processar um mini-batch e escrever .syn."""
        from synesis_coder.modes.abstract_mode import process_abstract

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Criar .bib com 2 entradas
            bib_path = _create_test_bib(tmp_path, [
                {
                    "bibref": "test_entry1",
                    "abstract": (
                        "Community trust is the most important factor for "
                        "social acceptance of wind energy projects. Local "
                        "ownership significantly reduces opposition."
                    ),
                },
                {
                    "bibref": "test_entry2",
                    "abstract": (
                        "Environmental concern drives public support for "
                        "renewable energy. Information campaigns increase "
                        "awareness and acceptance of solar panels."
                    ),
                },
            ])

            output_dir = tmp_path / "output"
            result = process_abstract(
                project_path=PROJECT_SOCIAL,
                bib_path=bib_path,
                output_dir=output_dir,
                concurrent=2,
                batch_size=10,
                per_reference=True,
            )

            assert "OK:" in result
            assert output_dir.exists()

            # Verificar que os .syn foram criados
            syn_files = list(output_dir.glob("*.syn"))
            assert len(syn_files) >= 1, f"Nenhum .syn gerado em {output_dir}"

    @requires_api_key
    def test_process_abstract_single_file(self):
        """process_abstract com per_reference=False deve gerar annotations.syn."""
        from synesis_coder.modes.abstract_mode import process_abstract

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            bib_path = _create_test_bib(tmp_path, [
                {
                    "bibref": "single_test",
                    "abstract": (
                        "Risk perception constrains acceptance of CCS technology. "
                        "Trust in regulatory institutions moderates this effect."
                    ),
                },
            ])

            output_dir = tmp_path / "output"
            process_abstract(
                project_path=PROJECT_SOCIAL,
                bib_path=bib_path,
                output_dir=output_dir,
                concurrent=1,
                batch_size=10,
                per_reference=False,
            )

            combined_file = output_dir / "annotations.syn"
            assert combined_file.exists(), "annotations.syn não foi criado"
            content = combined_file.read_text(encoding="utf-8")
            assert "SOURCE @single_test" in content
