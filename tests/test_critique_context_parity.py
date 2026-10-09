"""Paridade de contexto do critique (Estudo_Assimetria_Contexto_Critique, Fase 3).

O crítico julga `granularity` ("prejudica o reuso no corpus") sem ver a lista de
conceitos que o extrator viu, e seus critérios eram uma reimplementação parcial
de `_field_instruction`: um campo sem GUIDELINES sumia, e valores/faixas/BUNDLE
não chegavam. Todos offline.
"""

from __future__ import annotations

from pathlib import Path

from synesis.ast.nodes import FieldSpec, FieldType, OrderedValue, Scope, SourceLocation

from synesis_coder.prompt_builder import (
    _build_code_index_section,
    _build_critique_system_prompt,
    _build_topic_index_section,
)

LOC = SourceLocation(file=Path("t.synt"), line=1, column=1)


def _ctx(
    item_fields: dict,
    *,
    codes=("trust", "procedural_justice"),
    relations=None,
    bundles=(),
    required=(),
    topics=(),
) -> dict:
    return {
        "item_fields": item_fields,
        "chain_relations": relations or {},
        "required_item": list(required),
        "bundle_pairs": [list(b) for b in bundles],
        "code_index": {"codes": list(codes), "stats": {c: 1 for c in codes}, "empty": not codes},
        "topic_index": {"topics": list(topics), "empty": not topics},
        "project_description": "Projeto teste.",
    }


def _spec(name, ftype, **kw) -> FieldSpec:
    return FieldSpec(name=name, type=ftype, scope=Scope.ITEM, location=LOC, **kw)


class TestCriterios:
    def test_campo_sem_guidelines_nem_descricao_aparece(self):
        sp = _build_critique_system_prompt(_ctx({"marcador": _spec("marcador", FieldType.CODE)}))
        assert "marcador (CODE) [OPTIONAL]" in sp

    def test_valores_de_enumerated_chegam_ao_critico(self):
        f = _spec(
            "zona",
            FieldType.ENUMERATED,
            guidelines="Zona retórica.",
            values=[
                OrderedValue(index=0, label="objetivo", description="", location=LOC),
                OrderedValue(index=1, label="método", description="", location=LOC),
            ],
        )
        sp = _build_critique_system_prompt(_ctx({"zona": f}))
        assert "Allowed values" in sp and "objetivo" in sp and "método" in sp

    def test_faixa_de_scale_e_required(self):
        f = _spec("peso", FieldType.SCALE, guidelines="Peso.", format="0..3")
        sp = _build_critique_system_prompt(_ctx({"peso": f}, required=["peso"]))
        assert "Range: 0..3" in sp and "peso (SCALE) [REQUIRED]" in sp

    def test_bundle_chega_ao_critico(self):
        fs = {
            "a": _spec("a", FieldType.TEXT, guidelines="A."),
            "b": _spec("b", FieldType.TEXT, guidelines="B."),
        }
        sp = _build_critique_system_prompt(_ctx(fs, bundles=[("a", "b")]))
        assert "Bundled fields (BUNDLE): a + b" in sp

    def test_template_sem_chain_nao_tem_secao_de_relacoes(self):
        sp = _build_critique_system_prompt(
            _ctx(
                {"codigos": _spec("codigos", FieldType.CODE, guidelines="C.")},
                relations={"CAUSES": "x"},
            )
        )
        assert "Available relations" not in sp

    def test_chain_recebe_relacoes_do_proprio_campo(self):
        sp = _build_critique_system_prompt(
            _ctx({"rel": _spec("rel", FieldType.CHAIN, guidelines="R.",
                               relations={"INFLUENCES": "A muda B"})})
        )
        assert "INFLUENCES: A muda B" in sp

    def test_dois_chain_cada_um_com_suas_relacoes(self):
        """O ctx guarda só as relações do 1º CHAIN; cada campo usa as suas."""
        fs = {
            "causal": _spec("causal", FieldType.CHAIN, guidelines="C.",
                            relations={"CAUSA": "A causa B"}),
            "ligacoes": _spec("ligacoes", FieldType.CHAIN, guidelines="L."),
        }
        sp = _build_critique_system_prompt(_ctx(fs, relations={"CAUSA": "A causa B"}))
        bloco_ligacoes = sp.split("ligacoes (CHAIN)")[1].split("\n\n")[0]
        assert "CAUSA" in sp.split("ligacoes (CHAIN)")[0]
        assert "Available relations" not in bloco_ligacoes


class TestListaDeConceitos:
    def test_critico_ve_os_conceitos_existentes(self):
        sp = _build_critique_system_prompt(
            _ctx({"rel": _spec("rel", FieldType.CHAIN, guidelines="R.")})
        )
        assert "EXISTING PROJECT CONCEPTS" in sp and "procedural_justice" in sp

    def test_enquadramento_e_de_avaliacao_nao_de_extracao(self):
        sp = _build_critique_system_prompt(
            _ctx({"rel": _spec("rel", FieldType.CHAIN, guidelines="R.")})
        )
        assert "prefer these" not in sp
        assert "judging reuse" in sp and "Do not force reuse" in sp

    def test_sem_conceitos_sem_secao(self):
        sp = _build_critique_system_prompt(
            _ctx({"rel": _spec("rel", FieldType.CHAIN, guidelines="R.")}, codes=())
        )
        assert "EXISTING PROJECT CONCEPTS" not in sp

    def test_topicos_so_quando_ha_campo_topic_no_item(self):
        fs = {"rel": _spec("rel", FieldType.CHAIN, guidelines="R.")}
        assert "EXISTING TOPICS" not in _build_critique_system_prompt(_ctx(fs, topics=["Finanças"]))
        fs["tema"] = _spec("tema", FieldType.TOPIC, guidelines="T.")
        assert "EXISTING TOPICS (reference" in _build_critique_system_prompt(
            _ctx(fs, topics=["Finanças"])
        )


class TestExtracaoInalterada:
    """O raio de `_build_code_index_section` é alto (todos os prompts de extração):
    o texto da extração precisa sair IDÊNTICO ao anterior."""

    def test_texto_da_extracao_identico(self):
        idx = {"codes": ["a", "b"], "stats": {}, "empty": False}
        assert _build_code_index_section(idx) == (
            "EXISTING PROJECT CONCEPTS (prefer these; "
            "create new ones only when none of the existing ones apply):\n  a, b"
        )
        assert _build_topic_index_section({"topics": ["X"], "empty": False}) == (
            "EXISTING TOPICS (for TOPIC fields — prefer these):\n  X"
        )
