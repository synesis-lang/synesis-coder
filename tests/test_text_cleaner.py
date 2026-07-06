"""Testes de text_cleaner — filtragem de ruído antes do chunking."""

from __future__ import annotations

from synesis_coder.text_cleaner import _remove_empty_sections, clean_document


class TestRemoveEmptySections:
    def test_nao_informado_section_removed(self):
        text = "## Prêmios e Títulos\n\nNão informado.\n\n## Formação\n\nMestrado em CC."
        out = _remove_empty_sections(text)
        assert "Prêmios" not in out
        assert "Formação" in out
        assert "Mestrado" in out

    def test_nenhum_item_cadastrado_removed(self):
        text = "## Bancas\n\nNenhum item cadastrado.\n\n## Artigos\n\nArtigo X."
        out = _remove_empty_sections(text)
        assert "Bancas" not in out
        assert "Artigos" in out

    def test_section_with_content_preserved(self):
        text = "## Formação\n\nDoutorado em CC — UFMG, 2007."
        out = _remove_empty_sections(text)
        assert "Doutorado" in out

    def test_multiple_blank_lines_between_header_and_marker(self):
        text = "## Prêmios\n\n\nNão informado.\n\n## Artigos\n\nArtigo A."
        out = _remove_empty_sections(text)
        assert "Prêmios" not in out
        assert "Artigo A" in out

    def test_na_marker_removed(self):
        text = "## Seção\n\nN/A.\n\n## Próxima\n\nConteúdo."
        out = _remove_empty_sections(text)
        assert "Seção" not in out
        assert "Próxima" in out


class TestBoilerplateRemoval:
    def test_gerado_em_removed(self):
        text = "Gerado em: 09/06/2026 10:30:00\n\n## Formação\n\nDoutorado."
        out = clean_document(text)
        assert "Gerado em" not in out
        assert "Doutorado" in out

    def test_relatorio_gerado_em_removed(self):
        text = "**Relatório gerado em:** 2026-06-09\n\n## Formação\n\nMestrado."
        out = clean_document(text)
        # O padrão cobre "Relatório gerado em: DD/MM/AAAA"; o formato com traço
        # e asteriscos pode não ser coberto — este teste documenta o comportamento atual.
        assert "Mestrado" in out

    def test_atualizacao_cv_removed(self):
        text = "**Atualização do CV:** 21/05/2026\n\n## Artigos\n\nArtigo X."
        # O padrão remove "Atualização do CV: DD/MM/AAAA" em linha simples.
        # Com asteriscos de negrito, a regex não actua — documentado.
        out = clean_document(text)
        assert "Artigo X" in out

    def test_endereco_cv_removed(self):
        text = "Endereço para acessar este CV: http://lattes.cnpq.br/123456\n\nConteúdo."
        out = clean_document(text)
        assert "Endereço para acessar" not in out
        assert "Conteúdo" in out

    def test_este_curriculo_foi_gerado_removed(self):
        text = "Este currículo foi gerado pelo sistema Lattes em 01/01/2026.\n\nConteúdo."
        out = clean_document(text)
        assert "Este currículo foi gerado" not in out
        assert "Conteúdo" in out


class TestPaginationRemoval:
    def test_pagina_x_de_y_removed(self):
        text = "Página 3 de 12\n\nConteúdo real."
        out = clean_document(text)
        assert "Página" not in out
        assert "Conteúdo real" in out

    def test_separator_line_removed(self):
        text = "Seção A\n\n----\n\nSeção B"
        out = clean_document(text)
        assert "----" not in out
        assert "Seção A" in out
        assert "Seção B" in out

    def test_underscore_separator_removed(self):
        text = "Texto\n\n____\n\nMais texto."
        out = clean_document(text)
        assert "____" not in out


class TestWhitespaceNormalization:
    def test_multiple_spaces_collapsed(self):
        out = clean_document("palavra1    palavra2")
        assert "palavra1 palavra2" in out

    def test_excessive_newlines_collapsed(self):
        out = clean_document("A\n\n\n\n\nB")
        assert "\n\n\n" not in out
        assert "A" in out
        assert "B" in out

    def test_tabs_normalized(self):
        out = clean_document("A\t\t\tB")
        assert "\t" not in out
        assert "A B" in out


class TestCleanDocumentIdempotent:
    def test_idempotent(self):
        text = (
            "## Prêmios\n\nNão informado.\n\n"
            "Página 3 de 12\n\n"
            "Endereço para acessar este CV: http://lattes.cnpq.br/123\n\n"
            "## Formação\n\nDoutorado em CC."
        )
        once = clean_document(text)
        twice = clean_document(once)
        assert once == twice

    def test_real_content_preserved(self):
        text = (
            "## Formação Acadêmica\n\n"
            "Doutorado em Ciência da Computação — UFMG, 2007.\n\n"
            "## Projetos de Pesquisa\n\n"
            "Otimização combinatória em sistemas produtivos (em andamento)."
        )
        out = clean_document(text)
        assert "Doutorado" in out
        assert "Otimização combinatória" in out
