"""Lista de conceitos durante a campanha: cobertura (dataset) e versão por lote (document).

Todos offline. Os campos são identificados pelo TIPO no template, nunca pelo nome.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from synesis.ast.nodes import FieldSpec, FieldType, OrderedValue, Scope, SourceLocation

from synesis_coder.concept_index import (
    concepts_from_item_blocks,
    has_open_term_fields,
    refresh_every,
    with_concepts,
)
from synesis_coder.modes import document_mode as dm

LOC = SourceLocation(file=Path("t.synt"), line=1, column=1)


def _spec(name, ftype, **kw):
    return FieldSpec(name=name, type=ftype, scope=Scope.ITEM, location=LOC, **kw)


FIELDS = {
    "trecho": _spec("trecho", FieldType.QUOTATION),
    "relacoes": _spec("relacoes", FieldType.CHAIN, relations={"CAUSA": "A causa B"}),
    "ligacoes": _spec("ligacoes", FieldType.CHAIN),  # cadeia simples
    "codigos": _spec("codigos", FieldType.CODE),
    "emocao": _spec(
        "emocao",
        FieldType.CODE,
        values=[OrderedValue(index=0, label="medo", description="", location=LOC)],
    ),
}


class TestCamposAbertos:
    def test_lista_fechada_nao_conta(self):
        assert not has_open_term_fields({"emocao": FIELDS["emocao"], "trecho": FIELDS["trecho"]})
        assert has_open_term_fields({"codigos": FIELDS["codigos"]})


class TestExtracaoDeConceitos:
    BLOCO = (
        "ITEM @x\n"
        "    trecho: A pobreza causa evasão -> nada\n"
        "    relacoes: pobreza -> CAUSA -> evasão -> CAUSA -> pobreza_extrema\n"
        "    ligacoes: renda -> consumo\n"
        "    codigos: renda_baixa, pobreza\n"
        "    emocao: medo\n"
        "END ITEM"
    )

    def test_por_tipo_e_por_item_distinto(self):
        c = concepts_from_item_blocks([self.BLOCO], FIELDS)
        # cadeia qualificada: posições pares; simples: todas; CODE: por vírgula
        assert set(c) == {"pobreza", "evasão", "pobreza_extrema", "renda", "consumo", "renda_baixa"}
        assert c["pobreza"] == 1  # repetido no mesmo ITEM conta uma vez
        assert "CAUSA" not in c and "medo" not in c  # relação e lista fechada fora
        assert "A pobreza causa evasão" not in c  # QUOTATION nunca é termo

    def test_conta_itens_distintos(self):
        c = concepts_from_item_blocks([self.BLOCO, self.BLOCO], FIELDS)
        assert c["pobreza"] == 2

    def test_nova_versao_nao_altera_a_anterior(self):
        v1 = {"codes": ["a"], "stats": {"a": 3}, "empty": False}
        from collections import Counter

        v2 = with_concepts(v1, Counter({"b": 1, "a": 1}))
        assert v1 == {"codes": ["a"], "stats": {"a": 3}, "empty": False}
        assert v2["codes"] == ["a", "b"] and v2["stats"]["a"] == 4


class TestTamanhoDoLote:
    def test_padrao_e_variavel(self, monkeypatch):
        monkeypatch.delenv("SYNESIS_CODER_CONCEPT_REFRESH", raising=False)
        assert refresh_every(3) == 10 and refresh_every(16) == 16
        monkeypatch.setenv("SYNESIS_CODER_CONCEPT_REFRESH", "0")
        assert refresh_every(3) == 0
        monkeypatch.setenv("SYNESIS_CODER_CONCEPT_REFRESH", "abc")
        assert refresh_every(3) == 10


class TestDocumentoVersionadoPorLote:
    """Todo trecho de um lote vê a MESMA versão; só trechos válidos alimentam a seguinte."""

    def _rodar(self, n_trechos, monkeypatch, refresh="2", falha=()):
        monkeypatch.setenv("SYNESIS_CODER_CONCEPT_REFRESH", refresh)
        vistos: dict = {}

        async def fake_chunk(bibref, chunk, i, total, ctx, llm, sem):
            vistos[i] = list(ctx["code_index"]["codes"])
            await asyncio.sleep(0.001 * (n_trechos - i))  # termina fora de ordem
            bloco = f"ITEM @x\n    relacoes: c{i} -> CAUSA -> comum\nEND ITEM"
            return i, [bloco], i not in falha

        ctx = {
            "item_fields": FIELDS,
            "code_index": {"codes": ["base"], "stats": {"base": 1}, "empty": False},
        }
        progress = MagicMock()
        with patch.object(dm, "_process_chunk", fake_chunk):
            res, ok, fail = asyncio.run(
                dm._process_chunks_versioned(
                    "x", [f"t{i}" for i in range(n_trechos)], ctx, MagicMock(), 2, progress
                )
            )
        return vistos, res, ok, fail, ctx

    def test_lotes_veem_versoes_sucessivas(self, monkeypatch):
        vistos, res, ok, fail, ctx = self._rodar(5, monkeypatch)
        assert vistos[0] == vistos[1] == ["base"]  # lote 1: versão inicial
        assert vistos[2] == vistos[3] == ["base", "c0", "c1", "comum"]  # lote 2: + lote 1
        assert vistos[4] == ["base", "c0", "c1", "c2", "c3", "comum"]
        assert ok == 5 and fail == 0 and sorted(res) == [0, 1, 2, 3, 4]
        assert ctx["code_index"]["codes"] == ["base"]  # ctx original intacto

    def test_trecho_invalido_nao_entra_na_lista(self, monkeypatch):
        vistos, _, ok, fail, _ = self._rodar(4, monkeypatch, falha={1})
        assert "c1" not in vistos[2] and "c0" in vistos[2]
        assert ok == 3 and fail == 1

    def test_refresh_zero_mantem_lote_unico(self, monkeypatch):
        vistos, *_ = self._rodar(4, monkeypatch, refresh="0")
        assert all(v == ["base"] for v in vistos.values())


class TestDatasetCobertura:
    """O dataset carregava o projeto SEM anotações: lista de conceitos vazia."""

    def _rodar(self, item_fields):
        from synesis_coder.modes import dataset_mode as ds

        chamadas = []

        def fake_load(path, **kw):
            chamadas.append(kw["load_annotations"])
            return {
                "item_fields": item_fields,
                "code_index": {"codes": ["a"], "stats": {"a": 1}, "empty": False},
            }

        with (
            patch.object(ds, "load_project", fake_load),
            patch.object(ds, "parse_dataset_records", return_value=[]),
        ):
            msg = asyncio.run(
                ds._process_dataset_async(
                    Path("p.synp"), Path("out"), 1, False, None, "text", False
                )
            )
        return chamadas, msg

    def test_recarrega_com_anotacoes_quando_ha_vocabulario_aberto(self):
        chamadas, _ = self._rodar({"codigos": FIELDS["codigos"]})
        assert chamadas == [False, True]

    def test_nao_recarrega_sem_vocabulario_aberto(self):
        chamadas, msg = self._rodar({"emocao": FIELDS["emocao"]})
        assert chamadas == [False] and "Nenhum registro" in msg
