"""Testes para o modo document do synesis-coder.

Testes de split_into_chunks, merge_and_dedup e build_document_prompt
não chamam o LLM. Testes de integração requerem ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Caminhos de projetos e fixtures
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")

PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"
PROJECT_AIDS = CASES_DIR / "Sociology/iramuteq_aids_corpus/aids_corpus.synp"

HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

requires_api_key = pytest.mark.skipif(
    not HAS_API_KEY, reason="ANTHROPIC_API_KEY não disponível"
)

# Texto de entrevista simulada para testes
SAMPLE_INTERVIEW = """\
Entrevistador: Qual é a sua opinião sobre projetos de energia eólica na sua comunidade?

Entrevistado: Olha, eu acho que depende muito de como o projeto é apresentado para a comunidade.
Quando as pessoas não são consultadas desde o início, elas ficam desconfiadas e acabam se opondo.
A confiança é fundamental. Se a empresa responsável pelo projeto tem boa reputação e age de forma
transparente, as pessoas tendem a aceitar melhor.

Entrevistador: E os aspectos econômicos influenciam essa aceitação?

Entrevistado: Com certeza. Se a comunidade vai se beneficiar economicamente, seja através de
empregos ou de royalties, a aceitação aumenta bastante. Mas se os benefícios vão todos para
fora da região, aí surgem conflitos. A percepção de justiça distributiva é muito importante.

Entrevistador: E quanto aos impactos ambientais?

Entrevistado: Esse é um ponto delicado. Algumas pessoas se preocupam com o impacto visual,
com o ruído das turbinas e com os efeitos sobre a fauna local, especialmente as aves.
Mas outras veem o projeto como uma forma de combater as mudanças climáticas e aceitam
esses impactos como necessários. Depende muito do sistema de valores de cada pessoa.

Entrevistador: O processo de licenciamento importa?

Entrevistado: Muito. Quando o processo é claro, transparente e inclui a participação da
comunidade, as pessoas se sentem respeitadas. Mas quando é algo feito às escondidas,
pelos bastidores, cria uma resistência muito grande. A regulação precisa ser vista como
justa e eficiente.
"""


def _create_test_document(tmpdir: Path, content: str = SAMPLE_INTERVIEW) -> Path:
    """Cria um documento .txt temporário para testes."""
    doc_path = tmpdir / "test_document.txt"
    doc_path.write_text(content, encoding="utf-8")
    return doc_path


# ---------------------------------------------------------------------------
# Testes de split_into_chunks (sem LLM)
# ---------------------------------------------------------------------------


class TestSplitIntoChunks:
    """Testes para split_into_chunks()."""

    def test_short_text_returns_single_chunk(self):
        """Texto menor que chunk_size deve retornar um único chunk."""
        from synesis_coder.modes.document_mode import split_into_chunks

        text = "Parágrafo curto.\n\nOutro parágrafo curto."
        chunks = split_into_chunks(text, chunk_size=10000, overlap=1000)

        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_splits_into_multiple_chunks(self):
        """Texto longo deve ser dividido em múltiplos chunks."""
        from synesis_coder.modes.document_mode import split_into_chunks

        # Criar texto com 5 parágrafos de 1000 chars cada
        paragraphs = [f"Parágrafo {i}. " + "x" * 950 for i in range(5)]
        text = "\n\n".join(paragraphs)

        chunks = split_into_chunks(text, chunk_size=2500, overlap=500)

        assert len(chunks) > 1

    def test_overlap_content_appears_in_consecutive_chunks(self):
        """O overlap deve fazer com que conteúdo apareça em chunks consecutivos."""
        from synesis_coder.modes.document_mode import split_into_chunks

        paragraphs = [f"Parágrafo único número {i}. Conteúdo específico {i}." for i in range(6)]
        text = "\n\n".join(paragraphs)

        chunks = split_into_chunks(text, chunk_size=300, overlap=100)

        if len(chunks) > 1:
            # O último parágrafo do primeiro chunk deve aparecer no segundo
            # (ou haja overlap de algum conteúdo)
            assert len(chunks) >= 2

    def test_no_empty_chunks(self):
        """Nenhum chunk deve ser vazio."""
        from synesis_coder.modes.document_mode import split_into_chunks

        text = "\n\n".join([f"Parágrafo {i}. " + "a" * 500 for i in range(10)])
        chunks = split_into_chunks(text, chunk_size=1500, overlap=300)

        for chunk in chunks:
            assert chunk.strip() != ""

    def test_chunks_cover_all_content(self):
        """Todos os parágrafos originais devem aparecer em pelo menos um chunk."""
        from synesis_coder.modes.document_mode import split_into_chunks

        markers = [f"MARCADOR_{i}" for i in range(5)]
        paragraphs = [f"{m}. Texto do parágrafo {i}." for i, m in enumerate(markers)]
        text = "\n\n".join(paragraphs)

        chunks = split_into_chunks(text, chunk_size=200, overlap=50)
        combined = " ".join(chunks)

        for marker in markers:
            assert marker in combined, f"Marcador '{marker}' ausente nos chunks"

    def test_split_by_sentences_for_long_paragraph(self):
        """Parágrafo maior que chunk_size deve ser dividido por sentenças."""
        from synesis_coder.modes.document_mode import split_into_chunks

        # Um parágrafo muito longo (sem \n\n)
        long_para = ". ".join([f"Sentença número {i}" for i in range(100)]) + "."
        chunks = split_into_chunks(long_para, chunk_size=500, overlap=100)

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 600  # Margem para sentenças longas

    def test_fallback_for_text_without_headers(self):
        """Texto sem cabeçalhos Markdown deve usar algoritmo size-based (regressão)."""
        from synesis_coder.modes.document_mode import split_into_chunks

        # Texto corrido sem nenhum '#' — deve produzir o mesmo resultado que antes
        paragraphs = [f"Parágrafo {i}. " + "x" * 400 for i in range(6)]
        text = "\n\n".join(paragraphs)

        chunks = split_into_chunks(text, chunk_size=1500, overlap=300)
        assert len(chunks) > 1
        combined = " ".join(chunks)
        for i in range(6):
            assert f"Parágrafo {i}" in combined


# ---------------------------------------------------------------------------
# Testes de semantic chunking (sem LLM)
# ---------------------------------------------------------------------------


SAMPLE_MARKDOWN = """\
# Currículo Lattes — Prof. X

Informações gerais sobre o pesquisador.

## Formação acadêmica

Doutorado em Ciência da Computação pela USP (2005).
Mestrado em Matemática Aplicada pela UNICAMP (2001).

## Produção bibliográfica

Artigo 1. Título A. Revista X, 2020.
Artigo 2. Título B. Revista Y, 2021.
Artigo 3. Título C. Revista Z, 2022.

## Projetos de pesquisa

Projeto Alpha: Modelagem de redes complexas (2019–2022).
Projeto Beta: Otimização combinatória (2022–2024).
"""


class TestHasMarkdownStructure:
    """Testes para _has_markdown_structure()."""

    def test_doc_with_multiple_headers_returns_true(self):
        from synesis_coder.modes.document_mode import _has_markdown_structure
        assert _has_markdown_structure(SAMPLE_MARKDOWN) is True

    def test_plain_text_returns_false(self):
        from synesis_coder.modes.document_mode import _has_markdown_structure
        text = "Texto corrido sem nenhum cabeçalho.\n\nSegundo parágrafo."
        assert _has_markdown_structure(text) is False

    def test_single_header_returns_false_with_default_min(self):
        from synesis_coder.modes.document_mode import _has_markdown_structure
        text = "# Apenas um título\n\nCorpo do documento."
        assert _has_markdown_structure(text) is False

    def test_single_header_returns_true_with_min_1(self):
        from synesis_coder.modes.document_mode import _has_markdown_structure
        text = "# Apenas um título\n\nCorpo do documento."
        assert _has_markdown_structure(text, min_headers=1) is True

    def test_various_header_levels_detected(self):
        from synesis_coder.modes.document_mode import _has_markdown_structure
        text = "## Seção\n\nCorpo.\n\n### Subseção\n\nMais corpo."
        assert _has_markdown_structure(text) is True


class TestParseMarkdownSections:
    """Testes para _parse_markdown_sections()."""

    def test_returns_one_section_per_header(self):
        from synesis_coder.modes.document_mode import _parse_markdown_sections
        sections = _parse_markdown_sections(SAMPLE_MARKDOWN)
        # Espera: título principal + 3 seções ##
        assert len(sections) == 4

    def test_section_text_starts_with_header(self):
        from synesis_coder.modes.document_mode import _parse_markdown_sections
        sections = _parse_markdown_sections(SAMPLE_MARKDOWN)
        # Seções com cabeçalho devem ter header_line não vazio e section_text começa com ele
        for header_line, section_text in sections:
            if header_line:
                assert section_text.startswith(header_line.split()[0])  # começa com '#'

    def test_preamble_gets_empty_header(self):
        from synesis_coder.modes.document_mode import _parse_markdown_sections
        text = "Preâmbulo sem cabeçalho.\n\n## Seção 1\n\nCorpo."
        sections = _parse_markdown_sections(text)
        assert sections[0][0] == ""  # preâmbulo tem header vazio
        assert "Preâmbulo" in sections[0][1]

    def test_no_headers_returns_single_section(self):
        from synesis_coder.modes.document_mode import _parse_markdown_sections
        text = "Texto corrido.\n\nSem cabeçalhos."
        sections = _parse_markdown_sections(text)
        assert len(sections) == 1
        assert sections[0][0] == ""


class TestSplitByHeaders:
    """Testes para _split_by_headers() e dispatch em split_into_chunks."""

    def test_small_sections_grouped_into_one_chunk(self):
        """Seções menores que chunk_size devem ser agrupadas num único chunk."""
        from synesis_coder.modes.document_mode import split_into_chunks

        # Total ~800 chars, chunk_size=2000 → deve caber num único chunk
        text = (
            "## Seção A\n\nConteúdo A.\n\n"
            "## Seção B\n\nConteúdo B.\n\n"
            "## Seção C\n\nConteúdo C."
        )
        chunks = split_into_chunks(text, chunk_size=2000, overlap=200)
        assert len(chunks) == 1

    def test_large_section_subdivided_with_header_prefix(self):
        """Seção gigante deve ser subdividida e cada subchunk ter o cabeçalho."""
        from synesis_coder.modes.document_mode import split_into_chunks

        # Dois cabeçalhos para ativar o modo semântico (min_headers=2).
        # A segunda seção tem corpo com 100 entradas longas (~2000 chars).
        entries = ". ".join([f"Publicação {i}: título extenso sobre tema importante" for i in range(50)]) + "."
        text = f"## Introdução\n\nTexto curto.\n\n## Produção bibliográfica\n\n{entries}"

        # chunk_size=500 garante que a seção grande (>2000 chars) seja subdividida
        chunks = split_into_chunks(text, chunk_size=500, overlap=50)
        assert len(chunks) > 1
        combined = "\n".join(chunks)
        # O cabeçalho da seção grande deve aparecer em pelo menos um subchunk
        assert "## Produção bibliográfica" in combined

    def test_semantic_dispatch_activated_for_structured_doc(self):
        """Documento com ≥2 headers deve usar o modo semântico."""
        from synesis_coder.modes.document_mode import split_into_chunks, _has_markdown_structure

        assert _has_markdown_structure(SAMPLE_MARKDOWN) is True
        # Reduzir chunk_size para forçar múltiplos chunks
        chunks = split_into_chunks(SAMPLE_MARKDOWN, chunk_size=200, overlap=40)
        assert len(chunks) > 1

    def test_all_content_preserved_in_semantic_mode(self):
        """Nenhum conteúdo deve ser perdido no modo semântico."""
        from synesis_coder.modes.document_mode import split_into_chunks

        chunks = split_into_chunks(SAMPLE_MARKDOWN, chunk_size=300, overlap=50)
        combined = "\n".join(chunks)
        assert "Formação acadêmica" in combined
        assert "Produção bibliográfica" in combined
        assert "Projetos de pesquisa" in combined

    def test_size_based_fallback_unchanged(self):
        """Texto sem cabeçalhos deve produzir resultado coerente (fallback)."""
        from synesis_coder.modes.document_mode import split_into_chunks

        paras = [f"Parágrafo {i}. " + "y" * 300 for i in range(8)]
        text = "\n\n".join(paras)
        chunks = split_into_chunks(text, chunk_size=1000, overlap=200)
        combined = " ".join(chunks)
        for i in range(8):
            assert f"Parágrafo {i}" in combined


# ---------------------------------------------------------------------------
# Testes de merge_and_dedup (sem LLM)
# ---------------------------------------------------------------------------


class TestMergeAndDedup:
    """Testes para merge_and_dedup()."""

    def test_unique_items_all_preserved(self):
        """ITEMs com conteúdo diferente devem ser todos preservados."""
        from synesis_coder.modes.document_mode import merge_and_dedup

        items = [
            "ITEM @ref1\n    text: Community trust enables acceptance.\n    chain: Trust -> INFLUENCES -> Acceptance\nEND ITEM",
            "ITEM @ref1\n    text: Economic benefits increase participation.\n    chain: Economic_Benefit -> ENABLES -> Participation\nEND ITEM",
            "ITEM @ref1\n    text: Risk perception constrains deployment.\n    chain: Risk_Perception -> CONSTRAINS -> Deployment\nEND ITEM",
        ]

        result = merge_and_dedup(items)

        assert result.count("ITEM @ref1") == 3
        assert "Trust -> INFLUENCES -> Acceptance" in result
        assert "Economic_Benefit -> ENABLES -> Participation" in result
        assert "Risk_Perception -> CONSTRAINS -> Deployment" in result

    def test_identical_items_deduplicated(self):
        """ITEMs idênticos devem ser deduplicados."""
        from synesis_coder.modes.document_mode import merge_and_dedup

        item = "ITEM @ref1\n    text: Community trust enables acceptance.\n    chain: Trust -> INFLUENCES -> Acceptance\nEND ITEM"
        items = [item, item, item]  # mesma coisa 3 vezes

        result = merge_and_dedup(items)
        assert result.count("ITEM @ref1") == 1

    def test_normalized_duplicates_deduplicated(self):
        """ITEMs com mesma chain mas capitalização diferente devem ser deduplicados."""
        from synesis_coder.modes.document_mode import merge_and_dedup

        item1 = "ITEM @ref1\n    text: Trust enables acceptance.\n    chain: Trust -> INFLUENCES -> Acceptance\nEND ITEM"
        item2 = "ITEM @ref1\n    text: Trust enables acceptance.\n    chain: trust -> INFLUENCES -> acceptance\nEND ITEM"

        result = merge_and_dedup([item1, item2])
        assert result.count("ITEM @ref1") == 1

    def test_empty_list_returns_empty_string(self):
        """Lista vazia deve retornar string vazia."""
        from synesis_coder.modes.document_mode import merge_and_dedup

        assert merge_and_dedup([]) == ""

    def test_preserves_order_approximately(self):
        """ITEMs únicos devem ser preservados (ordem pode variar por dedup)."""
        from synesis_coder.modes.document_mode import merge_and_dedup

        items = [
            "ITEM @ref1\n    text: First item content here.\n    chain: A -> INFLUENCES -> B\nEND ITEM",
            "ITEM @ref1\n    text: Second item different content.\n    chain: C -> ENABLES -> D\nEND ITEM",
        ]
        result = merge_and_dedup(items)
        assert "A -> INFLUENCES -> B" in result
        assert "C -> ENABLES -> D" in result


class TestDedentBlock:
    """Testes para _dedent_block (remove indentação comum do bloco SOURCE)."""

    def test_no_indent_unchanged(self):
        from synesis_coder.modes.document_mode import _dedent_block
        block = "SOURCE @ref\n  field: x\nEND SOURCE"
        assert _dedent_block(block) == block

    def test_uniform_indent_removed(self):
        from synesis_coder.modes.document_mode import _dedent_block
        block = "    SOURCE @ref\n      field: x\n    END SOURCE"
        result = _dedent_block(block)
        assert result == "SOURCE @ref\n  field: x\nEND SOURCE"

    def test_preserves_relative_indent(self):
        from synesis_coder.modes.document_mode import _dedent_block
        block = "  SOURCE @ref\n    a: 1\n    b: 2\n  END SOURCE"
        result = _dedent_block(block)
        assert result == "SOURCE @ref\n  a: 1\n  b: 2\nEND SOURCE"


class TestExtractSourceBlock:
    """Testes da regex de extração de SOURCE (via _generate_source_block helpers)."""

    @staticmethod
    def _extract(raw: str):
        import re
        from synesis_coder.modes.document_mode import _dedent_block
        m = re.search(
            r"^[ \t]*SOURCE[ \t]+@\S+.*?^[ \t]*END[ \t]+SOURCE",
            raw, re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        return _dedent_block(m.group(0)) if m else None

    def test_plain_block(self):
        raw = "SOURCE @ref\n  field: x\nEND SOURCE"
        assert self._extract(raw) == raw

    def test_block_with_leading_explanation(self):
        raw = "Aqui está o bloco SOURCE:\n\nSOURCE @ref\n  field: x\nEND SOURCE"
        assert self._extract(raw) == "SOURCE @ref\n  field: x\nEND SOURCE"

    def test_indented_block_is_dedented(self):
        raw = "    SOURCE @ref\n      field: x\n    END SOURCE"
        assert self._extract(raw) == "SOURCE @ref\n  field: x\nEND SOURCE"

    def test_trailing_text_ignored(self):
        raw = "SOURCE @ref\n  field: x\nEND SOURCE\n\nEspero que ajude!"
        assert self._extract(raw) == "SOURCE @ref\n  field: x\nEND SOURCE"

    def test_end_source_extra_whitespace(self):
        raw = "SOURCE @ref\n  field: x\nEND  SOURCE"
        assert self._extract(raw) == "SOURCE @ref\n  field: x\nEND  SOURCE"


class TestPatchRequiredSourceFields:
    """Testes para _patch_required_source_fields (campos REQUIRED ausentes → NA)."""

    def test_missing_field_gets_na(self):
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\n  lattes_id: 123\nEND SOURCE"
        ctx = {"required_source": ["lattes_id", "cargo"]}
        result = _patch_required_source_fields(block, ctx)
        assert "cargo: NA" in result

    def test_patch_matches_existing_indent_two_spaces(self):
        """O campo inserido deve usar a MESMA indentação dos campos existentes."""
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\n  lattes_id: 123\nEND SOURCE"
        ctx = {"required_source": ["lattes_id", "cargo"]}
        result = _patch_required_source_fields(block, ctx)
        # 2 espaços, não 4 — senão o Indenter aninha o campo e ele some do SOURCE
        assert "\n  cargo: NA\n" in result
        assert "\n    cargo: NA\n" not in result

    def test_patch_matches_existing_indent_four_spaces(self):
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\n    lattes_id: 123\nEND SOURCE"
        ctx = {"required_source": ["lattes_id", "cargo"]}
        result = _patch_required_source_fields(block, ctx)
        assert "\n    cargo: NA\n" in result

    def test_present_field_not_duplicated(self):
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\n  cargo: Professor\nEND SOURCE"
        ctx = {"required_source": ["cargo"]}
        result = _patch_required_source_fields(block, ctx)
        assert result.count("cargo:") == 1
        assert "NA" not in result

    def test_empty_source_uses_default_indent(self):
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\nEND SOURCE"
        ctx = {"required_source": ["cargo"]}
        result = _patch_required_source_fields(block, ctx)
        assert "    cargo: NA" in result

    def test_no_required_fields_unchanged(self):
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        block = "SOURCE @ref\n  lattes_id: 123\nEND SOURCE"
        result = _patch_required_source_fields(block, {"required_source": []})
        assert result == block

    def test_patched_source_compiles(self):
        """Bloco corrigido deve compilar sem erro de indentação nem campo ausente."""
        import synesis
        from synesis_coder.modes.document_mode import _patch_required_source_fields

        template = """\
TEMPLATE t

SOURCE FIELDS
    REQUIRED lattes_id, cargo
END SOURCE FIELDS

ITEM FIELDS
    REQUIRED trecho
END ITEM FIELDS

FIELD lattes_id TYPE TEXT
    SCOPE SOURCE
END FIELD

FIELD cargo TYPE TEXT
    SCOPE SOURCE
END FIELD

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD
"""
        project = 'PROJECT p\n    TEMPLATE "t.synt"\nEND PROJECT'
        bib = "@misc{ref,\n  title={X}\n}"
        block = "SOURCE @ref\n  lattes_id: 123\nEND SOURCE"
        ctx = {"required_source": ["lattes_id", "cargo"]}
        patched = _patch_required_source_fields(block, ctx)
        annotation = patched + "\n\nITEM @ref\n  trecho: A quote.\nEND ITEM"

        result = synesis.load(
            project_content=project,
            template_content=template,
            annotation_contents={"a.syn": annotation},
            bibliography_content=bib,
        )
        # Não deve haver erro de "campo obrigatório ausente"
        diag = result.get_diagnostics(verbose=False)
        assert "cargo" not in diag or "ausente" not in diag


# ---------------------------------------------------------------------------
# Testes de build_document_prompt (sem LLM)
# ---------------------------------------------------------------------------


class TestDocumentPromptBuilder:
    """Testes para build_document_prompt()."""

    def test_prompt_structure(self):
        """Prompt deve ter system (cacheável) + user (dinâmico)."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_document_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_document_prompt(
            ctx, "entrevista_01", "Texto do chunk.", 0, 3
        )

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["cache"] is True
        assert messages[1]["role"] == "user"
        assert messages[1]["cache"] is False

    def test_user_message_contains_bibref_and_chunk(self):
        """Mensagem do usuário deve conter bibref e texto do chunk."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_document_prompt

        ctx = load_project(PROJECT_SOCIAL)
        bibref = "entrevista_01"
        chunk = "Conteúdo específico do chunk de teste."
        messages = build_document_prompt(ctx, bibref, chunk, 0, 1)

        user_content = messages[1]["content"]
        assert bibref in user_content
        assert chunk in user_content

    def test_chunk_position_in_user_message(self):
        """Mensagem deve indicar posição do chunk quando há múltiplos chunks."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_document_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_document_prompt(
            ctx, "entrevista_01", "Chunk texto.", 1, 5
        )

        user_content = messages[1]["content"]
        assert "2" in user_content  # chunk_index + 1
        assert "5" in user_content  # total_chunks

    def test_no_source_instruction_in_user_message(self):
        """Mensagem deve instruir a NÃO gerar bloco SOURCE."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_document_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_document_prompt(ctx, "ref1", "Chunk.", 0, 1)

        user_content = messages[1]["content"]
        assert "Do NOT generate a SOURCE block" in user_content

    def test_single_chunk_no_position_note(self):
        """Com apenas 1 chunk, não deve incluir nota de posição."""
        from synesis_coder.project_loader import load_project
        from synesis_coder.prompt_builder import build_document_prompt

        ctx = load_project(PROJECT_SOCIAL)
        messages = build_document_prompt(
            ctx, "ref1", "Chunk único.", chunk_index=0, total_chunks=1
        )

        user_content = messages[1]["content"]
        assert "Trecho" not in user_content


# ---------------------------------------------------------------------------
# Testes de read_document (sem LLM)
# ---------------------------------------------------------------------------


class TestReadDocument:
    """Testes para read_document()."""

    def test_reads_txt_file(self):
        """Deve ler arquivo .txt corretamente."""
        from synesis_coder.modes.document_mode import read_document

        with tempfile.TemporaryDirectory() as tmpdir:
            doc = _create_test_document(Path(tmpdir))
            text = read_document(doc)
            assert len(text) > 0
            assert "comunidade" in text.lower()

    def test_raises_on_missing_file(self):
        """Deve levantar FileNotFoundError para arquivo inexistente."""
        from synesis_coder.modes.document_mode import read_document

        with pytest.raises(FileNotFoundError):
            read_document(Path("d:/nao_existe/documento.txt"))

    def test_raises_on_empty_file(self):
        """Deve levantar ValueError para arquivo vazio."""
        from synesis_coder.modes.document_mode import read_document

        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "empty.txt"
            empty.write_text("", encoding="utf-8")
            with pytest.raises(ValueError, match="vazio"):
                read_document(empty)


# ---------------------------------------------------------------------------
# Testes de integração com LLM (requerem ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


class TestDocumentModeIntegration:
    """Testes de integração end-to-end com LLM real."""

    @requires_api_key
    def test_process_document_social_acceptance(self):
        """Deve processar documento e gerar .syn válido para social_acceptance."""
        import synesis
        from synesis_coder.modes.document_mode import process_document
        from synesis_coder.project_loader import load_project
        from synesis_coder.validator import _has_structural_errors

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            doc_path = _create_test_document(tmp_path)
            output_path = tmp_path / "entrevista_01.syn"

            process_document(
                project_path=PROJECT_SOCIAL,
                bibref="entrevista_01",
                input_path=doc_path,
                output_path=output_path,
                chunk_size=3000,
                overlap=500,
                concurrent=2,
            )

            assert output_path.exists(), ".syn não foi criado"
            content = output_path.read_text(encoding="utf-8")

            # Verificar estrutura básica
            assert "SOURCE @entrevista_01" in content
            assert "ITEM @entrevista_01" in content
            assert "END SOURCE" in content
            assert "END ITEM" in content

            # Validação via compilador
            ctx = load_project(PROJECT_SOCIAL, load_annotations=False)
            validation = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents={output_path.name: content},
                bibliography_content=ctx.get("bib_content"),
            )
            assert not _has_structural_errors(validation), (
                f"Output não compilou:\n{validation.get_diagnostics()}\n\n"
                f"Output:\n{content[:500]}"
            )

    @requires_api_key
    def test_process_document_verbose_format(self):
        """Formato verbose deve incluir header com estatísticas."""
        from synesis_coder.modes.document_mode import process_document

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            doc_path = _create_test_document(tmp_path)
            output_path = tmp_path / "test.syn"

            result = process_document(
                project_path=PROJECT_SOCIAL,
                bibref="entrevista_verbose",
                input_path=doc_path,
                output_path=output_path,
                chunk_size=3000,
                overlap=500,
                concurrent=1,
                format="verbose",
            )

            assert "# synesis-coder document" in result
            assert "# bibref: @entrevista_verbose" in result
            assert "tokens:" in result

    @requires_api_key
    def test_process_document_aids_corpus(self):
        """Deve funcionar com template de CHAIN em português (aids_corpus)."""
        from synesis_coder.modes.document_mode import process_document

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Texto simulado relevante para aids_corpus
            aids_text = (
                "Entrevistado: Quando soube que estava com HIV, senti muito medo do preconceito. "
                "As pessoas ainda associam a doença a comportamentos considerados inadequados. "
                "Isso dificulta muito o acesso ao tratamento porque as pessoas têm vergonha.\n\n"
                "Entrevistador: Como foi o suporte da família?\n\n"
                "Entrevistado: Minha família foi fundamental. O apoio deles me deu coragem "
                "para buscar tratamento. Sem esse suporte, eu provavelmente teria desistido."
            )
            doc_path = tmp_path / "participante.txt"
            doc_path.write_text(aids_text, encoding="utf-8")
            output_path = tmp_path / "participante_01.syn"

            process_document(
                project_path=PROJECT_AIDS,
                bibref="participante_01",
                input_path=doc_path,
                output_path=output_path,
                chunk_size=3000,
                overlap=500,
                concurrent=1,
            )

            assert output_path.exists()
            content = output_path.read_text(encoding="utf-8")
            assert "SOURCE @participante_01" in content
            assert "ITEM @participante_01" in content
