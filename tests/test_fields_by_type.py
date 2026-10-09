"""Campos identificados pelo TIPO do template, não pelo nome (auditoria de 2026-10-09).

Nos estudos de caso o QUOTATION se chama `text` (21 templates), `citation` (14),
`trecho` (11)...; o MEMO, `memo` (24) ou `note` (13). Todos offline.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from synesis.ast.nodes import FieldSpec, FieldType, Scope, SourceLocation

from synesis_coder.anchor_check import check_anchoring, format_report, items_without_excerpt
from synesis_coder.concept_index import definition_field
from synesis_coder.modes.critique_mode import _extract_item_text, quotation_fields
from synesis_coder.modes.document_mode import _chain_fields_of, _extract_chain_tuples
from synesis_coder.modes.incorporate_mode import _apply_revision_tags
from synesis_coder.prompt_builder import _build_critique_output_format, _format_critique_feedback
from synesis_coder.revision_vocab import is_meta_tag, meta_tags_for, reserved_field_collisions

LOC = SourceLocation(file=Path("t.synt"), line=1, column=1)


def _spec(name, ftype, scope=Scope.ITEM, **kw):
    return FieldSpec(name=name, type=ftype, scope=scope, location=LOC, **kw)


CTX = {"item_fields": {
    "citation": _spec("citation", FieldType.QUOTATION),
    "note": _spec("note", FieldType.MEMO),
    "relacoes": _spec("relacoes", FieldType.CHAIN, relations={"CAUSA": "x"}),
    "ligacoes": _spec("ligacoes", FieldType.CHAIN),
}}

ITEM = """ITEM @smith2024
    citation: Community trust drives acceptance.
    note: Trust is the mechanism.
    relacoes: trust -> CAUSA -> acceptance
END ITEM
"""

BIB = """@article{smith2024,
  title = {T},
  abstract = {We studied wind farms. Community trust drives acceptance. More work is needed.}
}
"""


class TestTrechoPeloTipo:
    def test_quotation_pelo_tipo_e_reserva_text(self):
        assert quotation_fields(CTX) == ["citation"]
        assert quotation_fields(None) == ["text"]

    def test_critique_acha_o_trecho_com_outro_nome(self):
        assert _extract_item_text(ITEM, CTX) == "Community trust drives acceptance."
        assert _extract_item_text(ITEM) == ""            # sem template: só `text`

    def test_anchor_verifica_o_campo_do_template(self):
        assert check_anchoring(ITEM, BIB, fields=["citation"]) == []
        ruim = ITEM.replace("drives acceptance", "blocks everything")
        assert len(check_anchoring(ruim, BIB, fields=["citation"])) == 1

    def test_anchor_informa_itens_sem_trecho_em_vez_de_pular_em_silencio(self):
        assert items_without_excerpt(ITEM, ["text"]) == 1
        rel = format_report([], 1, without_excerpt=1, fields=["text"])
        assert "0 ITEM(s) verificado(s)" in rel and "não verificados" in rel


class TestNoteDeclarado:
    def test_note_cede_ao_campo_declarado(self):
        assert is_meta_tag("note") and not is_meta_tag("note", ["citation", "note"])
        assert "note" not in meta_tags_for(["note"]) and "comment" in meta_tags_for(["note"])

    def test_colisoes_com_tags_que_o_coder_escreve(self):
        assert reserved_field_collisions(["comment", "Model", "note", "citation"]) == ["comment", "model"]

    def test_incorporate_aplica_correcao_do_campo_note(self):
        novo, mudou, _ = _apply_revision_tags(ITEM, {"note": "Trust explains acceptance."}, None)
        assert mudou == 0                                       # sem ctx: reserva antiga
        # a validação contra o projeto inteiro não é o assunto deste teste
        with patch("synesis_coder.modes.incorporate_mode._validate_item_block", return_value=True):
            novo, mudou, _ = _apply_revision_tags(ITEM, {"note": "Trust explains acceptance."},
                                                  {"item_fields": CTX["item_fields"]})
        assert mudou == 1 and "note: Trust explains acceptance." in novo

    def test_refine_mostra_a_dica_do_note_declarado(self):
        tags = {"reason": "unsupported", "note": "Trust explains acceptance."}
        assert "note:" not in _format_critique_feedback(tags)
        assert "note: Trust explains acceptance." in _format_critique_feedback(tags, ["note"])

    def test_instrucao_do_critico_reconhece_o_campo_note(self):
        assert "corrects the template field `note`" in _build_critique_output_format(CTX)
        sem_note = {"item_fields": {k: CTX["item_fields"][k] for k in ("citation", "relacoes")}}
        assert "NEVER output # $note:" in _build_critique_output_format(sem_note)


class TestDefinicaoPelaDescricao:
    def test_escolhe_pela_descricao(self):
        fs = {
            "significado": _spec("significado", FieldType.TEXT, Scope.ONTOLOGY,
                                 description="Definição do conceito no corpus."),
            "raciocinio": _spec("raciocinio", FieldType.TEXT, Scope.ONTOLOGY, description="Justificativa."),
        }
        assert definition_field(fs) == "significado"

    def test_unico_text_ou_nenhum(self):
        assert definition_field({"x": _spec("x", FieldType.TEXT, Scope.ONTOLOGY)}) == "x"
        assert definition_field({
            "a": _spec("a", FieldType.TEXT, Scope.ONTOLOGY), "b": _spec("b", FieldType.TEXT, Scope.ONTOLOGY)
        }) is None


class TestDeduplicacaoPorTipo:
    BLOCO = (
        "    relacoes: trust -> CAUSA -> acceptance\n"
        "    ligacoes: renda -> consumo -> poupanca\n"
        "    note: A -> B -> C não é cadeia\n"
    )

    def test_so_campos_chain_e_cada_um_no_seu_modo(self):
        tuplas = _extract_chain_tuples(self.BLOCO, _chain_fields_of(CTX))
        assert ("trust", "CAUSA", "acceptance") in tuplas
        assert ("renda", "", "consumo") in tuplas           # simples: sem relação
        assert len(tuplas) == 2                              # o MEMO com `->` não entra

    def test_sem_template_mantem_o_comportamento_antigo(self):
        assert len(_extract_chain_tuples(self.BLOCO)) == 3
