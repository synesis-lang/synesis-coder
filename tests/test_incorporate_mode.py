"""Testes para incorporate_mode — Fase 4 do pipeline ACT.

Todos os testes são unitários ou de integração leve (sem LLM).
Testes que usam synesis.load() precisam do compilador instalado mas não de API key.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from synesis_coder.modes.incorporate_mode import (
    _apply_revision_tags,
    _build_metrics_header,
    _replace_field_value,
    _strip_revision_metadata,
    _validate_item_block,
    process_incorporate,
)
from synesis_coder.synr_io import create_synr, write_synr

# ---------------------------------------------------------------------------
# Fixtures e constantes de teste
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"

_ITEM_CHAIN = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust enables social acceptance.
        note: Trust is a prerequisite for acceptance
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM
""")

_ITEM_NO_CHAIN = textwrap.dedent("""\
    ITEM @jones2020
        text: Local ownership reduces opposition.
        note: Ownership as key factor
    END ITEM
""")

_SYN_TWO_ITEMS = textwrap.dedent("""\
    SOURCE @smith2024
        description: Study on community trust.
        epistemic_model: Technology Acceptance Model
        method: survey
    END SOURCE

    ITEM @smith2024
        text: Community trust enables social acceptance.
        note: Trust is a prerequisite
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM

    ITEM @smith2024
        text: Environmental concern co-enables participation.
        note: Dual mechanism
        chain: Environmental_Concern -> ENABLES -> Participation
    END ITEM
""")


# ---------------------------------------------------------------------------
# _replace_field_value
# ---------------------------------------------------------------------------


class TestReplaceFieldValue:
    def test_replaces_existing_field(self):
        result = _replace_field_value(_ITEM_CHAIN, "chain", "Trust -> INFLUENCES -> Social_Acceptance")
        assert "Trust -> INFLUENCES -> Social_Acceptance" in result
        assert "Trust -> ENABLES -> Social_Acceptance" not in result

    def test_returns_none_when_field_not_found(self):
        result = _replace_field_value(_ITEM_CHAIN, "nonexistent_field", "value")
        assert result is None

    def test_preserves_indentation(self):
        result = _replace_field_value(_ITEM_CHAIN, "chain", "A -> B -> C")
        lines = result.splitlines()
        chain_line = next(l for l in lines if "chain" in l.lower() and "A -> B -> C" in l)
        assert chain_line.startswith("    ")

    def test_case_insensitive_field_name(self):
        result = _replace_field_value(_ITEM_CHAIN, "CHAIN", "New -> REL -> Value")
        assert "New -> REL -> Value" in result

    def test_ambiguous_multiple_occurrences_rejected(self):
        """Múltiplas ocorrências sem casamento de nó-fonte → rejeita.

        Antes da Fase 1 isto caía na PRIMEIRA ocorrência, destruindo um valor
        que a correção não endereçava. Ver estudo §5 (souza2022c).
        """
        block = textwrap.dedent("""\
            ITEM @ref
                note: first
                note: second
            END ITEM
        """)
        result = _replace_field_value(block, "note", "replaced")
        assert result is None

    def test_matches_occurrence_by_source_node(self):
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
                chain: B -> ENABLES -> Y
            END ITEM
        """)
        result = _replace_field_value(block, "chain", "B -> INHIBITS -> Y")
        assert "B -> INHIBITS -> Y" in result
        assert "A -> ENABLES -> X" in result  # intacta
        assert "B -> ENABLES -> Y" not in result

    def test_exact_value_match_disambiguates_non_chain(self):
        block = textwrap.dedent("""\
            ITEM @ref
                note: first
                note: second
            END ITEM
        """)
        result = _replace_field_value(block, "note", "second")
        assert result is not None
        assert result.count("second") == 1
        assert "first" in result

    def test_consumed_index_not_reused(self):
        """Correções de nós-fonte distintos consomem linhas distintas."""
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
                chain: B -> ENABLES -> Y
            END ITEM
        """)
        consumed: set[int] = set()
        first = _replace_field_value(block, "chain", "A -> INHIBITS -> X", consumed)
        assert first is not None
        assert len(consumed) == 1
        second = _replace_field_value(first, "chain", "B -> INHIBITS -> Y", consumed)
        assert second is not None
        assert len(consumed) == 2
        assert second.count("INHIBITS") == 2

    def test_shared_source_node_rejected(self):
        """Nó-fonte compartilhado não identifica o alvo → rejeita.

        Padrão normal de APPLIES no corpus. Escolher a primeira destruiria uma
        chain que a correção não endereçava (estudo §5, souza2022c).
        """
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
                chain: A -> ENABLES -> Y
            END ITEM
        """)
        result = _replace_field_value(block, "chain", "A -> INHIBITS -> X")
        assert result is None

    def test_more_corrections_than_occurrences_rejected(self):
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
            END ITEM
        """)
        consumed: set[int] = set()
        first = _replace_field_value(block, "chain", "A -> INHIBITS -> X", consumed)
        assert first is not None
        second = _replace_field_value(first, "chain", "A -> BLOCKS -> X", consumed)
        assert second is None

    def test_removal_sentinel_deletes_line(self):
        block = textwrap.dedent("""\
            ITEM @ref
                text: keep me
                chain: A -> ENABLES -> X
            END ITEM
        """)
        result = _replace_field_value(block, "chain", "none")
        assert result is not None
        assert "chain:" not in result
        assert "text: keep me" in result
        assert "none" not in result

    def test_removal_sentinel_parenthesized(self):
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
            END ITEM
        """)
        result = _replace_field_value(block, "chain", "(none)")
        assert result is not None
        assert "chain:" not in result

    def test_does_not_modify_unrelated_fields(self):
        result = _replace_field_value(_ITEM_CHAIN, "note", "New note content")
        assert "Trust -> ENABLES -> Social_Acceptance" in result
        assert "New note content" in result

    def test_preserves_other_lines(self):
        result = _replace_field_value(_ITEM_CHAIN, "note", "New note")
        assert "ITEM @smith2024" in result
        assert "END ITEM" in result
        assert "Community trust enables social acceptance." in result


# ---------------------------------------------------------------------------
# _strip_revision_metadata
# ---------------------------------------------------------------------------


class TestStripRevisionMetadata:
    def test_removes_phase_header(self):
        content = "# $phase: critique\n# $model: test\n# $timestamp: T\n\nSOURCE @ref\nEND SOURCE\n"
        result = _strip_revision_metadata(content)
        assert "# $phase:" not in result
        assert "# $model:" not in result
        assert "SOURCE @ref" in result

    def test_removes_revision_marker(self):
        content = "ITEM @ref\n    # REVISION\n    # $suspicion_score: 0.8\nEND ITEM\n"
        result = _strip_revision_metadata(content)
        assert "# REVISION" not in result
        assert "# $suspicion_score:" not in result
        assert "ITEM @ref" in result

    def test_removes_field_suggestion_tags(self):
        content = "ITEM @ref\n    chain: A -> B -> C\n    # REVISION\n    # $chain: A -> D -> C\nEND ITEM\n"
        result = _strip_revision_metadata(content)
        assert "# $chain:" not in result
        assert "# REVISION" not in result
        assert "chain: A -> B -> C" in result

    def test_preserves_regular_comments(self):
        """Comentários regulares (sem $) não devem ser removidos."""
        content = "# version: 2.0\nSOURCE @ref\nEND SOURCE\n"
        result = _strip_revision_metadata(content)
        assert "# version: 2.0" in result

    def test_collapses_consecutive_blank_lines(self):
        content = "A\n\n\n\nB\n"
        result = _strip_revision_metadata(content)
        assert "\n\n\n" not in result

    def test_preserves_single_blank_lines(self):
        content = "A\n\nB\n"
        result = _strip_revision_metadata(content)
        assert "\n\n" in result

    def test_dotted_metric_tags_removed(self):
        """Tags com ponto no namespace (# $metrics.acs:) também são removidas."""
        content = "ITEM @ref\n    # $metrics.acs: 0.9\nEND ITEM\n"
        result = _strip_revision_metadata(content)
        assert "# $metrics.acs:" not in result


# ---------------------------------------------------------------------------
# _build_metrics_header
# ---------------------------------------------------------------------------


class TestBuildMetricsHeader:
    def test_contains_required_metrics(self):
        metrics = {
            "total_items": 10,
            "items_with_revision": 6,
            "items_revised": 4,
            "fields_changed": 8,
            "fields_rejected": 2,
        }
        header = _build_metrics_header(metrics, Path("test.synr"), {})
        assert "# $metrics.total_items: 10" in header
        assert "# $metrics.items_with_revision: 6" in header
        assert "# $metrics.items_revised: 4" in header
        assert "# $metrics.fields_changed: 8" in header
        assert "# $metrics.fields_rejected: 2" in header

    def test_acs_calculation(self):
        metrics = {
            "total_items": 10, "items_with_revision": 5,
            "items_revised": 3, "fields_changed": 8, "fields_rejected": 2,
        }
        header = _build_metrics_header(metrics, Path("test.synr"), {})
        assert "# $metrics.acs: 0.800" in header

    def test_acs_one_when_no_fields(self):
        """ACS = 1.0 quando não há sugestões (divisão por zero protegida)."""
        metrics = {
            "total_items": 5, "items_with_revision": 0,
            "items_revised": 0, "fields_changed": 0, "fields_rejected": 0,
        }
        header = _build_metrics_header(metrics, Path("test.synr"), {})
        assert "# $metrics.acs: 1.000" in header

    def test_contains_source_filename(self):
        metrics = {
            "total_items": 1, "items_with_revision": 0,
            "items_revised": 0, "fields_changed": 0, "fields_rejected": 0,
        }
        header = _build_metrics_header(metrics, Path("my_review.synr"), {})
        assert "my_review.synr" in header

    def test_contains_timestamp(self):
        metrics = {
            "total_items": 1, "items_with_revision": 0,
            "items_revised": 0, "fields_changed": 0, "fields_rejected": 0,
        }
        header = _build_metrics_header(metrics, Path("test.synr"), {})
        assert "# $metrics.timestamp:" in header


# ---------------------------------------------------------------------------
# _apply_revision_tags
# ---------------------------------------------------------------------------


class TestApplyRevisionTags:
    def test_skips_meta_tags(self):
        """suspicion_score e reason não modificam campos do ITEM."""
        tags = {"suspicion_score": "0.9", "reason": "wrong_direction"}
        modified, changed, rejected = _apply_revision_tags(_ITEM_CHAIN, tags, ctx=None)
        assert modified == _ITEM_CHAIN
        assert changed == 0
        assert rejected == 0

    def test_applies_field_suggestion_without_ctx(self):
        """Sem ctx (sem projeto), aceita sugestão sem validação."""
        tags = {"chain": "Trust -> INFLUENCES -> Social_Acceptance"}
        modified, changed, rejected = _apply_revision_tags(_ITEM_CHAIN, tags, ctx=None)
        assert "Trust -> INFLUENCES -> Social_Acceptance" in modified
        assert changed == 1
        assert rejected == 0

    def test_field_not_in_item_ignored(self):
        """Sugestão para campo inexistente no ITEM é ignorada silenciosamente."""
        tags = {"nonexistent": "value"}
        modified, changed, rejected = _apply_revision_tags(_ITEM_CHAIN, tags, ctx=None)
        assert modified == _ITEM_CHAIN
        assert changed == 0
        assert rejected == 0

    def test_multiple_field_suggestions(self):
        """Múltiplas sugestões corrígíveis são aplicadas; note é meta-tag e ignorada."""
        tags = {
            "chain": "Trust -> INFLUENCES -> Social_Acceptance",
            # 'note' agora está em _META_TAGS — o LLM usa # $note: como raciocínio,
            # não como substituição de campo. Incorporate ignora.
            "note": "should be ignored",
            "suspicion_score": "0.75",
            "reason": "wrong_direction",
        }
        modified, changed, rejected = _apply_revision_tags(_ITEM_CHAIN, tags, ctx=None)
        assert "Trust -> INFLUENCES -> Social_Acceptance" in modified
        assert "should be ignored" not in modified  # note é meta-tag, não aplicada
        assert changed == 1

    def test_skips_metrics_namespace_tags(self):
        """Tags com prefixo metrics. são ignoradas."""
        tags = {"metrics.acs": "0.9", "chain": "A -> B -> C"}
        modified, changed, rejected = _apply_revision_tags(_ITEM_CHAIN, tags, ctx=None)
        assert changed == 1
        assert "A -> B -> C" in modified

    # --- Fase 1: integridade da incorporação --------------------------------

    def test_synr_header_tags_never_applied_as_fields(self):
        """model/timestamp/threshold são cabeçalho do .synr, não correções.

        Um template pode declarar um campo homônimo (projetos de ML, história,
        metodologia). Sem estes em _META_TAGS, o metadado da revisão viraria
        valor do campo do ITEM.
        """
        block = textwrap.dedent("""\
            ITEM @ref
                model: theoretical_sampling
                timestamp: 1998
                threshold: low
            END ITEM
        """)
        tags = {
            "model": "openai/gpt-5.6-luna",
            "timestamp": "2026-08-17T17:29:18Z",
            "threshold": "0.2",
        }
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)
        assert modified == block
        assert changed == 0
        assert "theoretical_sampling" in modified
        assert "openai/gpt-5.6-luna" not in modified

    def test_byte_identical_corrections_deduplicated(self):
        """Correções idênticas são rascunho do modelo, não duas correções."""
        block = textwrap.dedent("""\
            ITEM @souza2022c
                chain: estudo -> APPLIES -> qualidade
                chain: estudo -> APPLIES -> governanca
                chain: estudo -> APPLIES -> desempenho
            END ITEM
        """)
        tags = {
            "chain": "estudo -> APPLIES -> estruturas_de_governanca",
            "chain.1": "estudo -> APPLIES -> estruturas_de_governanca",
        }
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)
        # A 2ª é descartada como duplicata antes de qualquer tentativa; a 1ª é
        # rejeitada à parte por nó-fonte compartilhado entre as 3 chains.
        assert changed == 0
        assert rejected == 2
        assert "estruturas_de_governanca" not in modified

    def test_souza2022c_no_silent_data_loss(self):
        """Reproduz o caso do estudo §5: correção não destrói chain alheia.

        Antes da Fase 1 a correção casava pelo nó-fonte compartilhado, parava na
        PRIMEIRA linha e sobrescrevia `qualidade` — que não era o alvo — enquanto
        `governanca`, o alvo real, sobrevivia intacto.
        """
        block = textwrap.dedent("""\
            ITEM @souza2022c
                chain: estudo -> APPLIES -> qualidade
                chain: estudo -> APPLIES -> governanca
                chain: estudo -> APPLIES -> desempenho
            END ITEM
        """)
        tags = {"chain": "estudo -> APPLIES -> estruturas_de_governanca"}
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)

        # Três chains compartilham o nó-fonte 'estudo': o casamento por raiz NÃO
        # identifica o alvo. Rejeitar é a única ação segura — aplicar destruiria
        # `qualidade`, que a correção não endereçava.
        assert modified == block
        assert changed == 0
        assert rejected == 1
        assert "qualidade" in modified
        assert "desempenho" in modified

    def test_removal_sentinel_removes_chain(self):
        block = textwrap.dedent("""\
            ITEM @ref
                text: mantido
                chain: A -> ENABLES -> X
            END ITEM
        """)
        tags = {"chain": "none"}
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)
        assert changed == 1
        assert "chain:" not in modified
        assert "text: mantido" in modified

    def test_more_corrections_than_occurrences_counted_as_rejected(self):
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
            END ITEM
        """)
        tags = {
            "chain": "A -> INHIBITS -> X",
            "chain.1": "A -> BLOCKS -> X",
        }
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)
        assert changed == 1
        assert rejected == 1
        assert modified.count("chain:") == 1

    def test_two_corrections_distinct_source_nodes_both_applied(self):
        block = textwrap.dedent("""\
            ITEM @ref
                chain: A -> ENABLES -> X
                chain: B -> ENABLES -> Y
            END ITEM
        """)
        tags = {
            "chain": "A -> INHIBITS -> X",
            "chain.1": "B -> INHIBITS -> Y",
        }
        modified, changed, rejected = _apply_revision_tags(block, tags, ctx=None)
        assert changed == 2
        assert rejected == 0
        assert "A -> INHIBITS -> X" in modified
        assert "B -> INHIBITS -> Y" in modified


# ---------------------------------------------------------------------------
# process_incorporate — testes de integração (sem LLM)
# ---------------------------------------------------------------------------


class TestProcessIncorporate:
    def test_basic_incorporate_no_revision(self, tmp_path):
        """Arquivo .synr sem blocos REVISION gera .syn limpo sem metadados."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        result = process_incorporate(synr_path=synr_file, format="plain")

        output_file = tmp_path / "test.syn"
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        # Metadados .synr devem ter sido removidos
        assert "# $phase:" not in content
        assert "# $model:" not in content
        # Conteúdo Synesis deve estar presente
        assert "SOURCE @smith2024" in content
        assert "ITEM @smith2024" in content

    def test_incorporate_applies_field_suggestion(self, tmp_path):
        """Sugestão de campo é aplicada corretamente ao ITEM."""
        revisions = [
            {"suspicion_score": "0.9", "reason": "wrong_direction",
             "chain": "Trust -> INFLUENCES -> Social_Acceptance"},
            None,
        ]
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        process_incorporate(synr_path=synr_file, format="plain")

        output_file = tmp_path / "test.syn"
        content = output_file.read_text(encoding="utf-8")
        assert "Trust -> INFLUENCES -> Social_Acceptance" in content
        assert "Trust -> ENABLES -> Social_Acceptance" not in content

    def test_incorporate_strips_revision_blocks(self, tmp_path):
        """Blocos # REVISION são removidos do .syn final."""
        revisions = [
            {"suspicion_score": "0.8", "reason": "r", "note": "new note"},
            None,
        ]
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        process_incorporate(synr_path=synr_file, format="plain")

        content = (tmp_path / "test.syn").read_text(encoding="utf-8")
        assert "# REVISION" not in content
        assert "# $suspicion_score:" not in content
        assert "# $reason:" not in content

    def test_incorporate_metrics_header_present(self, tmp_path):
        """Cabeçalho # $metrics.* está presente no .syn final."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        process_incorporate(synr_path=synr_file, format="plain")

        content = (tmp_path / "test.syn").read_text(encoding="utf-8")
        assert "# $metrics.total_items:" in content
        assert "# $metrics.acs:" in content
        assert "# $metrics.timestamp:" in content

    def test_incorporate_custom_output_path(self, tmp_path):
        """--output personalizado é respeitado."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        custom_out = tmp_path / "custom" / "output.syn"
        process_incorporate(synr_path=synr_file, output_path=custom_out, format="plain")

        assert custom_out.exists()

    def test_incorporate_default_output_path(self, tmp_path):
        """Sem --output, gera arquivo com mesmo nome mas extensão .syn."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "review.synr"
        write_synr(synr_file, doc)

        process_incorporate(synr_path=synr_file, format="plain")

        assert (tmp_path / "review.syn").exists()

    def test_incorporate_items_without_revision_unchanged(self, tmp_path):
        """ITEM sem # REVISION tem conteúdo preservado intacto."""
        revisions = [
            {"chain": "Trust -> INFLUENCES -> Social_Acceptance"},
            None,
        ]
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        process_incorporate(synr_path=synr_file, format="plain")

        content = (tmp_path / "test.syn").read_text(encoding="utf-8")
        # Segundo ITEM (sem revisão) mantém chain original
        assert "Environmental_Concern -> ENABLES -> Participation" in content

    def test_incorporate_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            process_incorporate(synr_path=tmp_path / "nao_existe.synr")

    def test_incorporate_summary_mentions_paths(self, tmp_path):
        """Resumo da execução menciona arquivo de origem e saída."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "data.synr"
        write_synr(synr_file, doc)

        result = process_incorporate(synr_path=synr_file, format="plain")

        assert "data.synr" in result
        assert "data.syn" in result

    def test_incorporate_verbose_format(self, tmp_path):
        """Formato verbose inclui cabeçalho com metadados."""
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        synr_file = tmp_path / "test.synr"
        write_synr(synr_file, doc)

        result = process_incorporate(synr_path=synr_file, format="verbose")

        assert "# synesis-coder incorporate" in result
        assert "ACS" in result


# ---------------------------------------------------------------------------
# Integração com synesis.load() (projeto real, sem LLM)
# ---------------------------------------------------------------------------


class TestIncorporateWithSynesis:
    def test_output_compiles_cleanly(self, tmp_path):
        """O .syn final compilado por synesis.load() não tem erros estruturais."""
        try:
            import synesis

            from synesis_coder.project_loader import load_project
            from synesis_coder.validator import _has_structural_errors
        except ImportError:
            pytest.skip("synesis não disponível")

        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        social_dir = PROJECT_SOCIAL.parent
        syn_files = list(social_dir.glob("*.syn"))
        if not syn_files:
            pytest.skip("Nenhum .syn encontrado no projeto social_acceptance")

        syn_content = syn_files[0].read_text(encoding="utf-8")
        ctx = load_project(PROJECT_SOCIAL)

        # Criar .synr sem revisões a partir de conteúdo real
        header = {"phase": "critique", "model": "test", "timestamp": "T"}
        doc = create_synr(syn_content, header, [])
        synr_file = tmp_path / "real.synr"
        write_synr(synr_file, doc)

        process_incorporate(
            synr_path=synr_file,
            project_path=PROJECT_SOCIAL,
            format="plain",
        )

        output_file = tmp_path / "real.syn"
        assert output_file.exists()
        final_content = output_file.read_text(encoding="utf-8")

        # Validar que o .syn final compila sem erros estruturais
        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"final.syn": final_content},
            bibliography_content=ctx.get("bib_content"),
        )
        assert not _has_structural_errors(result), (
            f".syn final não compilou:\n{result.get_diagnostics()}"
        )

    def test_invalid_suggestion_rejected(self, tmp_path):
        """Sugestão que gera sintaxe inválida é rejeitada (campo preservado)."""
        try:
            from synesis_coder.project_loader import load_project
        except ImportError:
            pytest.skip("synesis não disponível")

        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        ctx = load_project(PROJECT_SOCIAL)

        # Tag com relação inválida (não definida no template) → deve ser rejeitada
        tags = {"chain": "SINTAXE_INVALIDA_@@@@!!!"}
        modified, changed, rejected = _apply_revision_tags(
            _ITEM_CHAIN, tags, ctx=ctx
        )
        # Deve ter rejeitado (sintaxe inválida não compila)
        assert rejected == 1
        assert changed == 0
        # Campo original preservado
        assert "Trust -> ENABLES -> Social_Acceptance" in modified
