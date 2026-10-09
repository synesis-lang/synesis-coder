"""Candidatos a variante (só diagnóstico) e o `normalize` lendo os campos pelo TIPO.

Todos offline.
"""

from __future__ import annotations

from pathlib import Path

from synesis.ast.nodes import FieldSpec, FieldType, OrderedValue, Scope, SourceLocation

from synesis_coder.modes.normalize_mode import (
    _build_revisions_for_doc,
    _extract_codes_from_item_block,
    _write_inventory_txt,
    apply_deterministic_normalization,
    build_code_inventory,
    term_fields_from_ctx,
)
from synesis_coder.synr_io import SynrDocument
from synesis_coder.variant_candidates import candidate_groups, variant_key

LOC = SourceLocation(file=Path("t.synt"), line=1, column=1)


class TestCandidatos:
    def test_grupos_reais_sao_apontados(self):
        """Os grupos encontrados no face85 e no Social Acceptance (2026-10-09)."""
        grupos = candidate_groups(
            [
                "questionário",
                "questionários",
                "percepção_temporal",
                "percepções_temporais",
                "construção_de_identidades",
                "construção_da_identidade",
                "Decision-Making",
                "Decision_Making",
                "outro",
            ]
        )
        assert ["questionário", "questionários"] in grupos
        assert ["percepção_temporal", "percepções_temporais"] in grupos
        assert ["construção_da_identidade", "construção_de_identidades"] in grupos
        assert ["Decision-Making", "Decision_Making"] in grupos
        assert not any("outro" in g for g in grupos)

    def test_reordenacao_nao_e_candidata(self):
        assert variant_key("controle_de_qualidade") != variant_key("qualidade_do_controle")

    def test_so_caixa_e_espaco_ja_sao_iguais_para_a_linguagem(self):
        """`normalize_code` já trata como o mesmo conceito: não é candidato."""
        assert candidate_groups(["Trust", "trust", "TRUST"]) == []

    def test_contraexemplos_sao_apenas_candidatos(self):
        """A regra junta palavras distintas — por isso ela NUNCA substitui.
        Aparecer como candidato é o comportamento correto; o teste de que nada
        é substituído está em TestNormalizePorTipo."""
        for par in (
            ["pais", "país"],
            ["avó", "avô"],
            ["secretaria", "secretária"],
            ["economic", "economics"],
        ):
            assert candidate_groups(par) == [sorted(par)]


def _spec(name, ftype, **kw):
    return FieldSpec(name=name, type=ftype, scope=Scope.ITEM, location=LOC, **kw)


CTX = {
    "item_fields": {
        "trecho": _spec("trecho", FieldType.QUOTATION),
        "relacoes": _spec("relacoes", FieldType.CHAIN, relations={"CAUSA": "x"}),
        "codigos": _spec("codigos", FieldType.CODE),
        "emocao": _spec(
            "emocao",
            FieldType.CODE,
            values=[OrderedValue(index=0, label="medo", description="", location=LOC)],
        ),
    }
}

BLOCO = """ITEM @x
    trecho: Pais e país.
    relacoes: Renda_Baixa -> CAUSA -> evasao
    codigos: Pobreza
    emocao: medo
END ITEM
"""


class TestNormalizePorTipo:
    def test_campos_pelo_tipo_lista_fechada_fora(self):
        assert term_fields_from_ctx(CTX) == {"relacoes": "CHAIN", "codigos": "CODE"}

    def test_sem_projeto_usa_os_nomes_tradicionais(self):
        assert term_fields_from_ctx(None) == {"chain": "CHAIN", "code": "CODE"}
        assert term_fields_from_ctx({"item_fields": {}}) == {"chain": "CHAIN", "code": "CODE"}

    def test_extrai_de_campos_com_nome_arbitrario(self):
        res = _extract_codes_from_item_block(BLOCO, term_fields_from_ctx(CTX))
        assert {(f, c) for f, c, _ in res} == {
            ("relacoes", "Renda_Baixa"),
            ("relacoes", "evasao"),
            ("codigos", "Pobreza"),
        }

    def test_nomes_tradicionais_ignoram_campos_arbitrarios(self):
        assert _extract_codes_from_item_block(BLOCO) == []

    def test_revisao_usa_o_nome_do_campo_e_nao_substitui_candidato(self, tmp_path):
        outro = BLOCO.replace("Renda_Baixa", "renda_baixa").replace("Pobreza", "pais")
        doc = SynrDocument(
            header={}, content=BLOCO + "\n" + outro + "\nITEM @y\n    codigos: país\nEND ITEM\n"
        )
        tf = term_fields_from_ctx(CTX)
        inv = build_code_inventory([(Path("a.synr"), doc)], tf)
        apply_deterministic_normalization(inv)
        revs = _build_revisions_for_doc(doc, Path("a.synr"), inv, tf)
        # caixa diferente (Renda_Baixa × renda_baixa) é o mesmo para a linguagem
        tags = [r for r in revs if r]
        assert tags and all(set(t) <= {"relacoes", "codigos", "phase"} for t in tags)
        # pais × país: candidatos, NUNCA substituídos um pelo outro
        assert not any(t.get("codigos") in ("pais", "país") for t in tags)
        out = tmp_path / "inv.txt"
        _write_inventory_txt(
            inv, out, candidate_groups(f for g in inv.values() for f in g.variants)
        )
        texto = out.read_text(encoding="utf-8")
        assert "Possíveis variantes" in texto and "pais | país" in texto
