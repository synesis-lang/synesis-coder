"""Fidelidade ao template: o que o autor escreve é o que o modelo recebe.

Cobre as mudanças de baixo risco do Estudo_Fidelidade_Template_Prompt
(synesis-planning/synesis-coder), todas offline:

- P1: OPTIONAL BUNDLE lido pelo loader e anunciado no prompt;
- P2: DESCRIPTION enviada junto com as GUIDELINES;
- P3: o prompt não acrescenta regras que não vêm do template;
- P4: campos de um bundle na ordem do BUNDLE (prompt, schema e .syn);
- P5: recuo uniforme das GUIDELINES no prompt;
- P7: `ontology --prompt-only` com a evidência real do corpus;
- P8: aviso para linha de GUIDELINES alterada pelo compilador.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from synesis_coder import template_lint
from synesis_coder.block_assembler import assemble_items
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import (
    build_abstract_prompt,
    build_abstract_values_prompt,
    describe_field,
)
from synesis_coder.prompt_dump import dump_prompt
from synesis_coder.schema_builder import build_abstract_schema

# `note` é definido ANTES de `chain`, mas o BUNDLE declara chain, status, note.
# A 1ª linha das GUIDELINES de `description` começa com a palavra-chave
# INCLUDE, que o compilador entrega em maiúsculas.
TEMPLATE = """TEMPLATE mini

SOURCE FIELDS
    REQUIRED description
END SOURCE FIELDS

FIELD description TYPE TEXT
    SCOPE SOURCE
    DESCRIPTION One-sentence summary of the study.
    GUIDELINES
        Include the technology domain when named.
        Keep it short.
    END GUIDELINES
END FIELD

ITEM FIELDS
    REQUIRED text
    OPTIONAL BUNDLE chain, status, note
END ITEM FIELDS

FIELD text TYPE QUOTATION
    SCOPE ITEM
    DESCRIPTION Literal excerpt from the abstract.
END FIELD

FIELD note TYPE MEMO
    SCOPE ITEM
    DESCRIPTION Justifies the chain immediately before it.
    GUIDELINES
        Quote the supporting expression.
        Explain the relation choice.
    END GUIDELINES
END FIELD

FIELD chain TYPE CHAIN
    SCOPE ITEM
    ARITY >= 2
    RELATIONS
        INFLUENCES: A affects B
    END RELATIONS
END FIELD

FIELD status TYPE ENUMERATED
    SCOPE ITEM
    VALUES
        Affirmed: The text asserts the relation
        Hypothetical: The text proposes the relation
    END VALUES
END FIELD
"""

BIB = """@article{smith2024,
  title = {T},
  abstract = {Trust affects acceptance of wind farms.}
}
"""

ANNOTATIONS = """SOURCE @smith2024
    description: Study of trust and wind farm acceptance.
END SOURCE

ITEM @smith2024
    text: Trust affects acceptance of wind farms.
    chain: trust -> INFLUENCES -> acceptance
    status: Affirmed
    note: "affects" asserts a directional effect.
END ITEM
"""


def _project(tmp_path: Path, with_annotations: bool) -> Path:
    (tmp_path / "mini.synt").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "refs.bib").write_text(BIB, encoding="utf-8")
    includes = 'INCLUDE BIBLIOGRAPHY "refs.bib"\n'
    if with_annotations:
        (tmp_path / "ann.syn").write_text(ANNOTATIONS, encoding="utf-8")
        includes += 'INCLUDE ANNOTATIONS "ann.syn"\n'
    synp = tmp_path / "mini.synp"
    synp.write_text(f'PROJECT mini\n\nTEMPLATE "mini.synt"\n{includes}\nEND PROJECT\n', encoding="utf-8")
    return synp


@pytest.fixture
def ctx(tmp_path):
    return load_project(_project(tmp_path, with_annotations=True))


@pytest.fixture
def ctx_empty(tmp_path):
    return load_project(_project(tmp_path, with_annotations=False))


def _system(messages) -> str:
    return messages[0]["content"]


# --- P1: OPTIONAL BUNDLE -----------------------------------------------------

class TestOptionalBundle:
    def test_loader_le_optional_bundle(self, ctx):
        assert ctx["optional_bundles"] == [("chain", "status", "note")]
        assert ctx["required_bundles"] == []
        assert ctx["bundle_pairs"] == [("chain", "status", "note")]

    def test_prompt_anuncia_o_bundle(self, ctx):
        system = _system(build_abstract_values_prompt(ctx, "smith2024", "x"))
        assert "Fields chain, status, note form a BUNDLE" in system
        assert "`bundles` array" in system
        assert "Use an empty array when the ITEM has none." in system
        assert "Each object's chain is ONE path" in system


# --- P2 e P5: DESCRIPTION + GUIDELINES, com recuo ----------------------------

class TestDescriptionEGuidelines:
    def test_describe_field_combina(self, ctx):
        note = describe_field(ctx["item_fields"]["note"])
        assert note.startswith("Definition: Justifies the chain immediately before it.")
        assert "Quote the supporting expression." in note

    def test_so_description(self, ctx):
        assert describe_field(ctx["item_fields"]["text"]) == "Literal excerpt from the abstract."

    def test_sem_texto_do_autor(self, ctx):
        assert describe_field(ctx["item_fields"]["chain"]) == ""

    def test_description_contida_nas_guidelines_nao_repete(self, ctx):
        spec = ctx["item_fields"]["note"]
        spec_copy = type(spec)(**{**spec.__dict__, "description": "Quote the supporting expression."})
        assert "Definition:" not in describe_field(spec_copy)

    def test_prompt_traz_a_definicao(self, ctx):
        system = _system(build_abstract_values_prompt(ctx, "smith2024", "x"))
        assert "Definition: Justifies the chain immediately before it." in system

    def test_linhas_seguintes_recuadas(self, ctx):
        system = _system(build_abstract_values_prompt(ctx, "smith2024", "x"))
        assert "\n    Explain the relation choice." in system
        assert "\nExplain the relation choice." not in system


# --- P3: nada de regra que não vem do template --------------------------------

class TestSemRegrasForaDoTemplate:
    def test_caminho_json(self, ctx):
        system = _system(build_abstract_values_prompt(ctx, "smith2024", "x"))
        assert "one finding" not in system
        assert "snake_case" not in system
        assert "Syntax: Concept_A" not in system
        assert "Format: a list of hops" in system

    def test_caminho_texto_livre(self, ctx):
        system = _system(build_abstract_prompt(ctx, "smith2024", "x"))
        assert "one finding" not in system
        assert "snake_case" not in system
        assert "note indicating" not in system
        assert "Syntax: Concept_A -> RELATION -> Concept_B" in system

    def test_idioma_dos_conceitos_so_com_a_lista(self, ctx, ctx_empty):
        ctx["output_language"] = "en"
        ctx_empty["output_language"] = "en"
        com_lista = _system(build_abstract_values_prompt(ctx, "smith2024", "x"))
        sem_lista = _system(build_abstract_values_prompt(ctx_empty, "smith2024", "x"))
        assert "EXISTING PROJECT CONCEPTS below" in com_lista
        assert "EXISTING PROJECT CONCEPTS" not in sem_lista


# --- P4: ordem do bundle -------------------------------------------------------

class TestOrdemDoBundle:
    def test_campos_na_ordem_do_bundle(self, ctx):
        assert list(ctx["item_fields"]) == ["text", "chain", "status", "note"]

    def test_schema_segue_a_ordem(self, ctx):
        props = build_abstract_schema(ctx)["properties"]["items"]["items"]["properties"]
        assert list(props) == ["text", "bundles"]
        assert list(props["bundles"]["items"]["properties"]) == ["chain", "status", "note"]

    def test_assembler_grava_na_ordem(self, ctx):
        """JSON no formato plano antigo (sem o array) continua montável."""
        data = {"items": [{
            "text": "Trust affects acceptance of wind farms.",
            "note": "affects is directional.",
            "chain": [{"source": "trust", "relation": "INFLUENCES", "target": "acceptance"}],
            "status": "Affirmed",
        }]}
        block = assemble_items(ctx, "smith2024", data)
        fields = [line.strip().split(":")[0] for line in block.splitlines()[1:-1]]
        assert fields == ["text", "chain", "status", "note"]


# --- P7: dump do ontology com evidência real -----------------------------------

class TestDumpOntology:
    def test_com_conceito_traz_a_evidencia(self, ctx):
        md = dump_prompt(ctx, mode="ontology", text="trust", path="json")
        assert "1 occurrence(s) across 1 distinct source(s)" in md
        assert "evidência do corpus: a mesma da execução" in md

    def test_sem_conceito_declara_a_omissao(self, ctx_empty):
        md = dump_prompt(ctx_empty, mode="ontology", text=None, path="json")
        assert "OMITIDA" in md


# --- P8: aviso de linha alterada --------------------------------------------------

class TestAvisoDeTemplate:
    def test_compilador_entrega_guidelines_intactas(self, ctx):
        """Desde o synesis 0.13.3 a linha iniciada por palavra-chave chega como escrita."""
        assert template_lint.altered_guideline_lines(TEMPLATE, ctx["field_specs"]) == []

    def test_detecta_linha_alterada(self, ctx):
        """Compilador anterior a 0.13.3 trocava `Include` por `INCLUDE`: o lint acusa."""
        specs = dict(ctx["field_specs"])
        spec = specs["description"]
        specs["description"] = type(spec)(**{
            **spec.__dict__,
            "guidelines": "INCLUDE the technology domain when named.\nKeep it short.",
        })
        altered = template_lint.altered_guideline_lines(TEMPLATE, specs)
        assert altered == [("description", "INCLUDE the technology domain when named.")]

    def test_template_limpo_nao_avisa(self, ctx):
        limpo = TEMPLATE.replace("Include the", "Mention the")
        specs = dict(ctx["field_specs"])
        spec = specs["description"]
        specs["description"] = type(spec)(**{
            **spec.__dict__,
            "guidelines": "Mention the technology domain when named.\nKeep it short.",
        })
        assert template_lint.altered_guideline_lines(limpo, specs) == []

    def test_avisa_uma_vez_por_template(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr(template_lint, "_already_warned", set())
        # O compilador atual não altera a linha: simula o anterior a 0.13.3.
        monkeypatch.setattr(
            template_lint, "altered_guideline_lines",
            lambda content, specs: [("description", "INCLUDE the technology domain when named.")],
        )
        synp = _project(tmp_path, with_annotations=False)
        with caplog.at_level(logging.WARNING, logger="synesis_coder.template_lint"):
            load_project(synp)
            load_project(synp)
        avisos = [r for r in caplog.records if "GUIDELINES" in r.getMessage()]
        assert len(avisos) == 1
        assert "INCLUDE the technology domain" in avisos[0].getMessage()


# --- P11: bundle como array de objetos ------------------------------------------

def _bundle(source, target, status="Affirmed", note="evidence: x"):
    return {
        "chain": [{"source": source, "relation": "INFLUENCES", "target": target}],
        "status": status,
        "note": note,
    }


def _is_valid(ctx, text: str) -> bool:
    """Valida pelo compilador; um texto válido não aciona o cliente (None)."""
    from synesis_coder.validator import validate_and_fix

    _out, ok = validate_and_fix(text, ctx, None, max_tries=0)
    return ok


SOURCE_BLOCK = "SOURCE @smith2024\n    description: d\nEND SOURCE\n\n"


class TestBundleComoObjeto:
    def test_schema_do_bundle(self, ctx):
        from synesis_coder.schema_builder import bundle_groups

        assert bundle_groups(ctx) == [("bundles", ("chain", "status", "note"), False)]
        item = build_abstract_schema(ctx)["properties"]["items"]["items"]
        bundles = item["properties"]["bundles"]
        assert bundles["type"] == "array"
        assert "minItems" not in bundles  # OPTIONAL BUNDLE: pode vir vazio
        obj = bundles["items"]
        assert obj["required"] == ["chain", "status", "note"]
        assert obj["additionalProperties"] is False
        # Dentro do bundle nada é nullable: um objeto é um bundle completo.
        assert None not in obj["properties"]["status"]["enum"]
        assert obj["properties"]["note"] == {"type": "string"}
        assert "bundles" in item["required"]

    def test_bundle_obrigatorio_exige_um_objeto(self, ctx):
        from synesis_coder.schema_builder import build_item_schema

        ctx = {**ctx, "required_bundles": ctx["optional_bundles"], "optional_bundles": []}
        item = build_item_schema(ctx)["properties"]["items"]["items"]
        assert item["properties"]["bundles"]["minItems"] == 1

    def test_chave_desloca_quando_o_template_tem_campo_bundles(self, ctx):
        from synesis_coder.schema_builder import bundle_groups

        fields = dict(ctx["item_fields"])
        fields["bundles"] = fields["text"]
        assert bundle_groups({**ctx, "item_fields": fields})[0][0] == "_bundles"

    def test_varios_bundles_no_mesmo_item(self, ctx):
        data = {"items": [{
            "text": "Trust affects acceptance of wind farms.",
            "bundles": [_bundle("trust", "acceptance"), _bundle("cost", "acceptance", "Hypothetical")],
        }]}
        block = assemble_items(ctx, "smith2024", data)
        fields = [line.strip().split(":")[0] for line in block.splitlines()[1:-1]]
        assert fields == ["text", "chain", "status", "note", "chain", "status", "note"]
        assert block.count("ITEM @smith2024") == 1  # trecho não se repete
        assert _is_valid(ctx, SOURCE_BLOCK + block)

    def test_caminhos_desconectados_viram_bundles_separados(self, ctx):
        bundle = _bundle("community_acceptance", "energy_transition")
        bundle["chain"].append(
            {"source": "community_acceptance", "relation": "INFLUENCES", "target": "adoption"}
        )
        block = assemble_items(ctx, "smith2024", {"items": [{"text": "T.", "bundles": [bundle]}]})
        assert block.count("chain:") == 2
        assert block.count("status:") == 2 and block.count("note:") == 2
        assert "community_acceptance -> INFLUENCES -> adoption" in block
        assert _is_valid(ctx, SOURCE_BLOCK + block)

    def test_caminho_contiguo_nao_divide(self, ctx):
        bundle = _bundle("a", "b")
        bundle["chain"].append({"source": "b", "relation": "INFLUENCES", "target": "c"})
        block = assemble_items(ctx, "smith2024", {"items": [{"text": "T.", "bundles": [bundle]}]})
        assert block.count("chain:") == 1
        assert "a -> INFLUENCES -> b -> INFLUENCES -> c" in block

    def test_array_vazio_e_item_sem_bundle(self, ctx):
        block = assemble_items(ctx, "smith2024", {"items": [{"text": "T.", "bundles": []}]})
        assert "chain:" not in block and "status:" not in block and "note:" not in block


# --- P12: correção pelo caminho JSON -----------------------------------------------

INVALID_ITEM = (
    SOURCE_BLOCK
    + "ITEM @smith2024\n    text: T.\n    chain: trust -> INFLUENCES -> acceptance\nEND ITEM\n"
)
VALID_ITEM = (
    SOURCE_BLOCK
    + "ITEM @smith2024\n    text: T.\n    chain: trust -> INFLUENCES -> acceptance\n"
    "    status: Affirmed\n    note: evidence: x\nEND ITEM\n"
)


class _TextFixClient:
    """Cliente mínimo: registra as correções em texto livre."""

    def __init__(self, reply: str = VALID_ITEM):
        self.reply = reply
        self.text_fixes = 0

    def fix(self, *args, **kwargs):
        self.text_fixes += 1
        return self.reply

    async def fix_async(self, *args, **kwargs):
        self.text_fixes += 1
        return self.reply


class TestCorrecaoJson:
    def test_usa_o_gancho_json_e_nao_o_texto_livre(self, ctx):
        from synesis_coder.validator import validate_and_fix

        client = _TextFixClient()
        calls = []

        def json_fix(previous, errors, temperature):
            calls.append((previous, errors, temperature))
            return VALID_ITEM

        out, ok = validate_and_fix(INVALID_ITEM, ctx, client, json_fix=json_fix)
        assert ok and out == VALID_ITEM
        assert client.text_fixes == 0
        assert calls and "status" in calls[0][1]  # o diagnóstico chega ao gancho

    def test_gancho_sem_resposta_cai_no_texto_livre(self, ctx):
        from synesis_coder.validator import validate_and_fix

        client = _TextFixClient()
        _out, ok = validate_and_fix(INVALID_ITEM, ctx, client, json_fix=lambda *a: None)
        assert ok and client.text_fixes == 1

    def test_gancho_com_erro_cai_no_texto_livre(self, ctx):
        from synesis_coder.validator import validate_and_fix

        def broken(*args):
            raise RuntimeError("provedor fora")

        client = _TextFixClient()
        _out, ok = validate_and_fix(INVALID_ITEM, ctx, client, json_fix=broken)
        assert ok and client.text_fixes == 1

    def test_versao_assincrona(self, ctx):
        import asyncio

        from synesis_coder.validator import validate_and_fix_async

        client = _TextFixClient()

        async def json_fix(previous, errors, temperature):
            return VALID_ITEM

        _out, ok = asyncio.run(validate_and_fix_async(
            INVALID_ITEM, ctx, client, json_fix=json_fix,
        ))
        assert ok and client.text_fixes == 0

    def test_mensagens_da_correcao(self):
        from synesis_coder.validator import json_fix_messages

        messages = [
            {"role": "system", "content": "SYS", "cache": True},
            {"role": "user", "content": "<abstract>x</abstract>", "cache": False},
        ]
        fixed = json_fix_messages(messages, "PREV", "ERRS")
        assert fixed[0] == messages[0]  # system idêntico: casa com o cache
        assert fixed[1]["content"].startswith("<abstract>x</abstract>")
        assert "PREV" in fixed[1]["content"] and "ERRS" in fixed[1]["content"]
        assert messages[1]["content"] == "<abstract>x</abstract>"  # original intacto

    def test_gancho_monta_pelo_assembler(self, ctx):
        from synesis_coder.validator import make_json_fix

        class _JsonClient:
            def call_json(self, messages, schema, temperature=None, correction=False):
                self.correction = correction
                return {"items": [{"text": "T.", "bundles": [_bundle("trust", "acceptance")]}]}

        client = _JsonClient()
        fix = make_json_fix(
            client, [{"role": "user", "content": "u"}], {},
            lambda data: assemble_items(ctx, "smith2024", data),
        )
        text = fix("prev", "errs", 0.0)
        assert client.correction is True
        assert "status: Affirmed" in text and "note: evidence: x" in text


class TestMarcaDeCorrecaoNoClienteJson:
    def _client(self):
        from unittest.mock import MagicMock, patch

        from synesis_coder.llm_client import LLMClient

        with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="k"):
            client = LLMClient(model="claude-sonnet-4-6", backend="anthropic")
        client._client = MagicMock()
        client._rate_limit_enabled = False
        client.supports_json_schema = lambda: True
        seen = []

        def fake_inner(*args, **kwargs):
            seen.append(getattr(client._correction_local, "is_correction", False))
            client._correction_local.is_correction = False
            return '{"items": []}'

        client._call_sync_inner = fake_inner
        return client, seen

    def test_call_json_marca_so_quando_pedido(self):
        client, seen = self._client()
        client.call_json([{"role": "user", "content": "x"}], {})
        client.call_json([{"role": "user", "content": "x"}], {}, correction=True)
        assert seen == [False, True]

    def test_call_json_async_marca_so_quando_pedido(self):
        import asyncio

        client, seen = self._client()
        asyncio.run(client.call_json_async([{"role": "user", "content": "x"}], {}))
        asyncio.run(client.call_json_async(
            [{"role": "user", "content": "x"}], {}, correction=True,
        ))
        assert seen == [False, True]
