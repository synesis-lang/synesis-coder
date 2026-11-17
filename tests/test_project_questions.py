"""Questões de competência (bloco QUESTIONS do .synp) no contexto do projeto.

Etapa Q do plano ROLE/EXPORT: as questões entram no mesmo "PROJECT CONTEXT" do
DESCRIPTION. Um projeto sem QUESTIONS — ou um compilador sem o bloco — gera o
texto de sempre, e por isso os prompts desses projetos não mudam.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from synesis_coder.project_loader import (
    _compose_project_context,
    _project_questions,
    load_project,
)


def _linked(description=None, questions=None):
    project = SimpleNamespace(description=description)
    if questions is not None:
        project.questions = [SimpleNamespace(id=i, text=t) for i, t in questions]
    return SimpleNamespace(project=project)


class TestCompose:
    def test_without_questions_is_the_description(self):
        assert _compose_project_context("Corpus X.", []) == "Corpus X."

    def test_without_anything_is_none(self):
        assert _compose_project_context(None, []) is None
        assert _compose_project_context("", []) is None

    def test_questions_follow_the_description(self):
        text = _compose_project_context("Corpus X.", [("Q1", "Uma?"), ("Q2", "Outra?")])
        assert text.startswith("Corpus X.\n\nCompetency questions")
        assert text.endswith("- Q1: Uma?\n- Q2: Outra?")

    def test_questions_without_description(self):
        text = _compose_project_context(None, [("Q1", "Uma?")])
        assert text.startswith("Competency questions")


class TestProjectQuestions:
    def test_old_compiler_without_attribute(self):
        assert _project_questions(_linked("x")) == []

    def test_no_linked_project(self):
        assert _project_questions(None) == []

    def test_malformed_lines_are_left_out(self):
        linked = _linked("x", [("Q1", "Uma?"), ("", ""), ("Q3", "")])
        assert _project_questions(linked) == [("Q1", "Uma?")]


def _compiler_has_questions() -> bool:
    import synesis.ast.nodes as nodes

    return hasattr(nodes, "QuestionNode")


@pytest.mark.skipif(not _compiler_has_questions(), reason="synesis sem o bloco QUESTIONS")
def test_questions_reach_project_context(tmp_path):
    (tmp_path / "t.synt").write_text(
        "SOURCE FIELDS\n    OPTIONAL description\nEND SOURCE FIELDS\n\n"
        "FIELD description TYPE TEXT\n    SCOPE SOURCE\nEND FIELD\n\n"
        "ITEM FIELDS\n    REQUIRED note\nEND ITEM FIELDS\n\n"
        "FIELD note TYPE MEMO\n    SCOPE ITEM\nEND FIELD\n",
        encoding="utf-8",
    )
    (tmp_path / "r.bib").write_text("@article{a1, title={T}, author={A}, year={2024}}\n", encoding="utf-8")
    (tmp_path / "a.syn").write_text(
        "SOURCE @a1\n    description: d\nEND SOURCE\nITEM @a1\n    note: n\nEND ITEM\n", encoding="utf-8"
    )
    (tmp_path / "p.synp").write_text(
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\nINCLUDE ANNOTATIONS "a.syn"\n\n'
        "DESCRIPTION\nCorpus de teste.\nEND DESCRIPTION\n\n"
        "QUESTIONS\n    Q1: Quais notas aparecem?\nEND QUESTIONS\nEND PROJECT\n",
        encoding="utf-8",
    )
    ctx = load_project(tmp_path / "p.synp")
    assert ctx["project_questions"] == [("Q1", "Quais notas aparecem?")]
    assert ctx["project_description"].startswith("Corpus de teste.")
    assert "- Q1: Quais notas aparecem?" in ctx["project_description"]
