"""Testes de _collect_includes — resolução das diretivas INCLUDE do .synp.

Regressão de dois defeitos que faziam anotações e ontologia serem descartadas
silenciosamente, deixando `code_index`/`ontology_index` vazios:

1. GLOB: `INCLUDE ANNOTATIONS "annotations/*.syn"` era testado com
   `Path.is_file()`, que é sempre False para um padrão — todo o corpus anotado
   sumia sem warning.
2. SHARED: a regex não cobria a palavra `SHARED`, então
   `INCLUDE SHARED ONTOLOGY "../ontologia.syno"` nunca casava e a ontologia
   compartilhada nunca era carregada.

O impacto ia além dos modos ontology/normalize/critique: `code_index` alimenta
o prompt de extração ("conceitos existentes"), então a extração rodava sem o
vocabulário acumulado que as GUIDELINES mandam reutilizar.
"""

from __future__ import annotations

from pathlib import Path

from synesis_coder.project_loader import _collect_includes


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestGlobExpansion:
    def test_annotations_glob_is_expanded(self, tmp_path: Path):
        _write(tmp_path / "annotations/a.syn", "SOURCE @a\nEND SOURCE\n")
        _write(tmp_path / "annotations/b.syn", "SOURCE @b\nEND SOURCE\n")
        project = 'TEMPLATE "t.synt"\nINCLUDE ANNOTATIONS "annotations/*.syn"\n'

        ann, _onto, _bib = _collect_includes(project, tmp_path)

        assert len(ann) == 2, f"glob não expandido: {list(ann)}"
        assert any("a.syn" in k for k in ann)
        assert any("b.syn" in k for k in ann)
        assert "@a" in "".join(ann.values())

    def test_literal_path_still_works(self, tmp_path: Path):
        """Caminho literal (sem glob) não pode regredir."""
        _write(tmp_path / "notes.syn", "SOURCE @x\nEND SOURCE\n")
        project = 'TEMPLATE "t.synt"\nINCLUDE ANNOTATIONS "notes.syn"\n'

        ann, _onto, _bib = _collect_includes(project, tmp_path)

        assert len(ann) == 1
        assert "@x" in "".join(ann.values())

    def test_glob_without_matches_yields_empty(self, tmp_path: Path):
        (tmp_path / "annotations").mkdir()
        project = 'TEMPLATE "t.synt"\nINCLUDE ANNOTATIONS "annotations/*.syn"\n'

        ann, _onto, _bib = _collect_includes(project, tmp_path)

        assert ann == {}

    def test_glob_does_not_escape_project_dir(self, tmp_path: Path):
        """`../*.syn` não pode arrastar arquivos de fora do projeto."""
        _write(tmp_path / "outside.syn", "SOURCE @out\nEND SOURCE\n")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project = 'TEMPLATE "t.synt"\nINCLUDE ANNOTATIONS "../*.syn"\n'

        ann, _onto, _bib = _collect_includes(project, project_dir)

        assert ann == {}, f"glob escapou do projeto: {list(ann)}"


class TestSharedOntology:
    def test_shared_ontology_is_collected(self, tmp_path: Path):
        """`INCLUDE SHARED ONTOLOGY` com alvo fora do projeto é autorizado."""
        _write(tmp_path / "ontologia.syno", "ONTOLOGY c\n  d: x\nEND ONTOLOGY\n")
        project_dir = tmp_path / "Dados_Lattes"
        project_dir.mkdir()
        project = (
            'TEMPLATE "t.synt"\nINCLUDE SHARED ONTOLOGY "../ontologia.syno"\n'
        )

        _ann, onto, _bib = _collect_includes(project, project_dir)

        assert len(onto) == 1, "SHARED ONTOLOGY não reconhecida"
        assert "ONTOLOGY c" in "".join(onto.values())

    def test_plain_ontology_still_works(self, tmp_path: Path):
        _write(tmp_path / "local.syno", "ONTOLOGY z\n  d: y\nEND ONTOLOGY\n")
        project = 'TEMPLATE "t.synt"\nINCLUDE ONTOLOGY "local.syno"\n'

        _ann, onto, _bib = _collect_includes(project, tmp_path)

        assert len(onto) == 1

    def test_non_shared_ontology_cannot_escape(self, tmp_path: Path):
        """Sem SHARED, alvo externo continua recusado (ESCAPES_PROJECT)."""
        _write(tmp_path / "ontologia.syno", "ONTOLOGY c\n  d: x\nEND ONTOLOGY\n")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project = 'TEMPLATE "t.synt"\nINCLUDE ONTOLOGY "../ontologia.syno"\n'

        _ann, onto, _bib = _collect_includes(project, project_dir)

        assert onto == {}, "ontologia externa aceita sem SHARED"


class TestCombinedDirectives:
    def test_realistic_project_collects_all_three(self, tmp_path: Path):
        """Cenário do case study: glob + SHARED ONTOLOGY + bibliography."""
        _write(tmp_path / "ontologia.syno", "ONTOLOGY c\n  d: x\nEND ONTOLOGY\n")
        project_dir = tmp_path / "Dados"
        _write(project_dir / "annotations/r1.syn", "SOURCE @r1\nEND SOURCE\n")
        _write(project_dir / "annotations/r2.syn", "SOURCE @r2\nEND SOURCE\n")
        _write(project_dir / "refs.bib", "@Article{k, title={T}}\n")
        project = (
            'TEMPLATE "t.synt"\n'
            'INCLUDE ANNOTATIONS "annotations/*.syn"\n'
            'INCLUDE SHARED ONTOLOGY "../ontologia.syno"\n'
            'INCLUDE BIBLIOGRAPHY "refs.bib"\n'
        )

        ann, onto, bib = _collect_includes(project, project_dir)

        assert len(ann) == 2
        assert len(onto) == 1
        assert bib is not None and "@Article" in bib
