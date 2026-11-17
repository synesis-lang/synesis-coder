"""Etapas B4 e B5: fases de taxonomia e de sinônimos, e `review` por amostragem.

O modelo é simulado (respostas JSON fixas); compilador, mesclagem, validação
do conjunto, manifesto e gravação são reais.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from synesis.compiler import SynesisCompiler

from synesis_coder import manifest as mf
from synesis_coder import phase_run
from synesis_coder.modes import phase_mode, review_mode, synonym_mode, taxonomy_mode
from synesis_coder.vocabulary import similarity_key

TEMPLATE = """\
ITEM FIELDS
    REQUIRED trecho, codigo
END ITEM FIELDS

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD

FIELD codigo TYPE CODE
    SCOPE ITEM
END FIELD

ONTOLOGY FIELDS
    REQUIRED definicao
    OPTIONAL topico, tipo_de, parte_de, usado_por, nome_antigo, justificativa
END ONTOLOGY FIELDS

FIELD definicao TYPE TEXT
    SCOPE ONTOLOGY
    ROLE NOTE DEFINITION
END FIELD

FIELD topico TYPE TOPIC
    SCOPE ONTOLOGY
END FIELD

FIELD tipo_de TYPE CODE
    SCOPE ONTOLOGY
    ROLE HIERARCHY KIND
END FIELD

FIELD parte_de TYPE CODE
    SCOPE ONTOLOGY
    ROLE HIERARCHY PART
END FIELD

FIELD usado_por TYPE TEXT
    SCOPE ONTOLOGY
    ROLE LABEL ALTERNATIVE
END FIELD

FIELD nome_antigo TYPE TEXT
    SCOPE ONTOLOGY
    ROLE LABEL HIDDEN
END FIELD

FIELD justificativa TYPE TEXT
    SCOPE ONTOLOGY
END FIELD
"""

BIB = "@article{a1, title={T}, author={A}, year={2024}}\n@article{a2, title={U}, author={B}, year={2025}}\n"

ANNOTATIONS = """\
SOURCE @a1
END SOURCE

ITEM @a1
    trecho: SMS com link falso para roubar senha.
    codigo: smishing, url_maliciosa
END ITEM

ITEM @a1
    trecho: Ligação de falso banco.
    codigo: vishing
END ITEM

SOURCE @a2
END SOURCE

ITEM @a2
    trecho: Mensagens de smishing em massa.
    codigo: smishings, phishing
END ITEM

ITEM @a2
    trecho: Golpe do abate de porcos.
    codigo: pig_butchering, fraude_online
END ITEM
"""

SYNO = """\
ONTOLOGY phishing
    definicao: Fraude por comunicação enganosa.
    topico: golpes
END ONTOLOGY

ONTOLOGY smishing
    definicao: Phishing por SMS.
    topico: golpes
END ONTOLOGY

ONTOLOGY vishing
    definicao: Phishing por voz.
    topico: golpes
END ONTOLOGY

ONTOLOGY fraude_online
    definicao: Fraude cometida pela internet.
    topico: golpes
END ONTOLOGY

ONTOLOGY url_maliciosa
    definicao: Endereço que leva a página falsa.
    topico: anatomia
END ONTOLOGY

ONTOLOGY pig_butchering
    definicao: Golpe de investimento de longo prazo.
    topico: investimento
END ONTOLOGY

ONTOLOGY pig-butchering
    definicao: Golpe do abate de porcos.
    topico: investimento
END ONTOLOGY
"""

PHASES = """
QUESTIONS
    Q1: Quais evidências tratam de phishing ou de seus subtipos?
END QUESTIONS

PHASE ONTOLOGY sinonimos
    FIELDS usado_por, nome_antigo
    INPUT definicao, topico
END PHASE

PHASE ONTOLOGY taxonomia
    FIELDS tipo_de, parte_de, justificativa
    INPUT definicao, topico
END PHASE
"""


def _project(root: Path, template: str = TEMPLATE, syno: str = SYNO, phases: str = PHASES) -> Path:
    for name, text in (("t.synt", template), ("r.bib", BIB), ("a.syn", ANNOTATIONS), ("o.syno", syno)):
        (root / name).write_bytes(text.encode("utf-8"))
    synp = root / "p.synp"
    synp.write_bytes((
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
        'INCLUDE ANNOTATIONS "a.syn"\nINCLUDE ONTOLOGY "o.syno"\n' + phases + "END PROJECT\n"
    ).encode("utf-8"))
    return synp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("SYNESIS_CODER_PHASE_") or key in ("SYNESIS_CODER_BACKEND", "SYNESIS_CODER_MODEL"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _client(**_kw):
    client = MagicMock()
    client.usage.summary_line.return_value = "tokens 0"
    client.fix_async = AsyncMock(side_effect=lambda text, *a, **k: text)
    return client


class Model:
    """Respostas fixas por passada; registra as mensagens recebidas."""

    def __init__(self, first=None, second=None, synonyms=None, parent_block=None):
        self.first = first or {"links": [], "new_parents": [], "top": []}
        self.second = second or {"links": [], "new_parents": [], "top": []}
        self.synonyms = synonyms or {"groups": []}
        self.parent_block = parent_block or (lambda name: f"ONTOLOGY {name}\n    definicao: Conceito amplo {name}.\nEND ONTOLOGY")
        self.messages: list = []
        self.generated: list = []

    def run(self, synp: Path, phase: str, **kwargs):
        async def ask(client, messages, schema):
            self.messages.append(messages)
            if phase == "sinonimos":
                return self.synonyms
            return self.second if "SECOND PASS" in messages[0]["content"] else self.first

        async def generate(code, ctx, semantic, client):
            self.generated.append((code, semantic))
            return self.parent_block(code)

        with patch.object(taxonomy_mode, "ask_json", ask), \
             patch.object(synonym_mode, "ask_json", ask), \
             patch.object(taxonomy_mode, "_generate_ontology_syno", generate), \
             patch.object(phase_run, "LLMClient", side_effect=_client), \
             patch.object(taxonomy_mode, "warn_schema_fallbacks", MagicMock()), \
             patch.object(synonym_mode, "warn_schema_fallbacks", MagicMock()):
            return phase_mode.run_phase(synp, phase, concurrent=1, **kwargs)


def _syno(root: Path) -> str:
    return (root / "o.syno").read_text(encoding="utf-8")


def _manifest(root: Path, phase: str) -> dict:
    return json.loads((root / f"o.{phase}.manifest.json").read_text(encoding="utf-8"))


def _compile_codes(synp: Path) -> list:
    result = SynesisCompiler(synp).compile()
    vr = result.validation_result
    return [e.CODE for e in vr.errors + vr.warnings]


FIRST = {
    "links": [
        {"child": "smishing", "parent": "phishing", "species": "KIND", "test": "todo smishing é phishing"},
        {"child": "vishing", "parent": "phishing", "species": "KIND", "test": "todo vishing é phishing"},
        {"child": "url_maliciosa", "parent": "phishing", "species": "PART", "test": "a URL é parte do ataque"},
        {"child": "inexistente", "parent": "phishing", "species": "KIND", "test": "?"},
    ],
    "new_parents": [
        {"name": "golpe digital", "species": "KIND", "children": ["phishing", "fraude_online"],
         "justification": "phishing e fraude online são golpes digitais", "questions": ["Q1"]},
    ],
    "top": [{"concept": "pig_butchering", "reason": "tipo de golpe de investimento, sem pai na tabela"}],
}


# --------------------------------------------------------------- taxonomia

class TestTaxonomy:
    def test_links_new_parent_and_manifest(self, tmp_path):
        synp = _project(tmp_path)
        model = Model(first=FIRST)
        report = model.run(synp, "taxonomia")
        text = _syno(tmp_path)
        assert "ONTOLOGY smishing\n    definicao: Phishing por SMS.\n    topico: golpes\n    tipo_de: phishing\nEND ONTOLOGY" in text
        assert "    parte_de: phishing\n" in text
        # pai novo: bloco completo, com o nome normalizado, e o filho ligado a ele
        assert "ONTOLOGY golpe_digital\n    definicao: Conceito amplo golpe_digital.\n" in text
        assert "ONTOLOGY phishing\n    definicao: Fraude por comunicação enganosa.\n    topico: golpes\n    tipo_de: golpe_digital\n" in text
        # nenhum .syn muda; o projeto compila sem erro e sem ciclo
        assert (tmp_path / "a.syn").read_bytes() == ANNOTATIONS.encode("utf-8")
        codes = _compile_codes(synp)
        assert not [c for c in codes if c in ("SYNESIS_E101", "SYNESIS_E119")]
        # o único sem fundamentação é o que já era (pig-butchering, sem menção): o pai novo se fundamenta
        result = SynesisCompiler(synp).compile()
        ungrounded = [w.to_cli_line() for w in result.validation_result.warnings if w.CODE == "SYNESIS_E120"]
        assert len(ungrounded) == 1 and "pig-butchering" in ungrounded[0]
        data = _manifest(tmp_path, "taxonomia")
        new = data["proposals"]["new:golpe_digital"]
        assert new["state"] == "applied" and new["questions"] == ["Q1"]
        assert new["derived_evidence"]  # trechos dos descendentes
        assert data["notes"]["top"]["pig_butchering"].startswith("tipo de golpe")
        assert data["notes"]["precheck"] == {"definicao": "producible"}
        assert "unplaced" in report

    def test_parent_definition_uses_descendant_excerpts(self, tmp_path):
        model = Model(first=FIRST)
        model.run(_project(tmp_path), "taxonomia")
        code, semantic = model.generated[0]
        assert code == "golpe_digital"
        assert any("narrower concept (derived evidence)" in ex for ex in semantic["examples"])

    def test_second_run_is_empty_diff(self, tmp_path):
        synp = _project(tmp_path)
        Model(first=FIRST).run(synp, "taxonomia")
        before = (tmp_path / "o.syno").read_bytes()
        model = Model(first=FIRST)
        assert "nothing to do" in model.run(synp, "taxonomia")
        assert model.messages == []
        assert (tmp_path / "o.syno").read_bytes() == before

    def test_filled_field_is_not_overwritten(self, tmp_path):
        syno = SYNO.replace("    definicao: Phishing por voz.\n", "    definicao: Phishing por voz.\n    tipo_de: fraude_online\n")
        synp = _project(tmp_path, syno=syno)
        Model(first=FIRST).run(synp, "taxonomia")
        assert "    tipo_de: fraude_online\n" in _syno(tmp_path)
        reason = _manifest(tmp_path, "taxonomia")["proposals"]["vishing KIND phishing"]["reason"]
        assert "already filled" in reason

    def test_new_generations_zero_disables_creation(self, tmp_path):
        synp = _project(tmp_path)
        model = Model(first=FIRST)
        report = model.run(synp, "taxonomia", options={"new_generations": 0})
        assert "golpe_digital" not in _syno(tmp_path)
        assert "parent creation off" in report
        assert "maxItems" in json.dumps(model.messages) or "not allowed" in model.messages[0][0]["content"]

    def test_required_alternative_label_blocks_creation_and_says_why(self, tmp_path):
        template = TEMPLATE.replace("    REQUIRED definicao\n    OPTIONAL topico, tipo_de, parte_de, usado_por,",
                                    "    REQUIRED definicao, usado_por\n    OPTIONAL topico, tipo_de, parte_de,")
        syno = SYNO.replace("END ONTOLOGY", "    usado_por: x\nEND ONTOLOGY")
        # rótulos repetidos dariam AmbiguousLabel; cada bloco ganha o seu
        syno = "\n".join(line.replace("usado_por: x", f"usado_por: rotulo{i}") for i, line in enumerate(syno.splitlines())) + "\n"
        synp = _project(tmp_path, template=template, syno=syno)
        report = Model(first=FIRST).run(synp, "taxonomia")
        assert "golpe_digital" not in _syno(tmp_path)
        assert "a synonym cannot be fabricated" in report
        assert "    tipo_de: phishing\n" in _syno(tmp_path)  # reconhecer pais continua

    def test_template_without_topic_runs(self, tmp_path):
        template = TEMPLATE.replace("FIELD topico TYPE TOPIC", "FIELD topico TYPE TEXT")
        synp = _project(tmp_path, template=template)
        report = Model(first=FIRST).run(synp, "taxonomia")
        assert "size (60)" in report
        assert "    tipo_de: phishing\n" in _syno(tmp_path)

    def test_single_child_parent_is_accepted_without_min_children(self, tmp_path):
        first = {"links": [], "top": [], "new_parents": [
            {"name": "golpe_de_investimento", "species": "KIND", "children": ["pig_butchering"],
             "justification": "todo pig butchering é golpe de investimento", "questions": []}]}
        synp = _project(tmp_path)
        Model(first=first).run(synp, "taxonomia")
        assert "ONTOLOGY golpe_de_investimento\n" in _syno(tmp_path)
        _project(tmp_path)  # de novo, com a política
        (tmp_path / "o.taxonomia.manifest.json").unlink()
        report = Model(first=first).run(synp, "taxonomia", options={"min_children": 2})
        assert "ONTOLOGY golpe_de_investimento\n" not in _syno(tmp_path)
        assert "--min-children 2" in report

    def test_invalid_new_parent_falls_with_its_links(self, tmp_path):
        synp = _project(tmp_path)
        bad = lambda name: f"ONTOLOGY {name}\n    definicao: x\n    campo_inexistente: y\nEND ONTOLOGY"  # noqa: E731
        Model(first=FIRST, parent_block=bad).run(synp, "taxonomia")
        text = _syno(tmp_path)
        assert "golpe_digital" not in text
        assert "    tipo_de: phishing\n" in text  # as outras ligações ficam
        props = _manifest(tmp_path, "taxonomia")["proposals"]
        assert props["new:golpe_digital"]["state"] == "rejected"
        assert props["phishing KIND golpe_digital"]["state"] == "rejected"

    def test_new_parent_named_like_existing_concept_is_reused(self, tmp_path):
        first = {"links": [], "top": [], "new_parents": [
            {"name": "phishing", "species": "KIND", "children": ["smishing"], "justification": "", "questions": []}]}
        synp = _project(tmp_path)
        Model(first=first).run(synp, "taxonomia")
        assert _syno(tmp_path).count("ONTOLOGY phishing\n") == 1
        assert "    tipo_de: phishing\n" in _syno(tmp_path)

    def test_inverse_links_do_not_form_a_cycle(self, tmp_path):
        first = {"new_parents": [], "top": [], "links": [
            {"child": "smishing", "parent": "phishing", "species": "KIND", "test": ""},
            {"child": "phishing", "parent": "smishing", "species": "KIND", "test": ""}]}
        synp = _project(tmp_path)
        Model(first=first).run(synp, "taxonomia")
        assert "tipo_de: smishing" not in _syno(tmp_path)
        assert "SYNESIS_E119" not in _compile_codes(synp)

    def test_second_pass_links_concepts_of_different_groups(self, tmp_path):
        first = {"new_parents": [], "top": [{"concept": "pig_butchering", "reason": "topo do grupo"}], "links": [
            {"child": "smishing", "parent": "phishing", "species": "KIND", "test": ""},
            {"child": "url_maliciosa", "parent": "phishing", "species": "PART", "test": ""}]}
        second = {"new_parents": [], "top": [], "links": [
            {"child": "phishing", "parent": "fraude_online", "species": "KIND", "test": "todo phishing é fraude online"}]}
        synp = _project(tmp_path)
        model = Model(first=first, second=second)
        model.run(synp, "taxonomia", options={"group_size": 3})
        assert any("SECOND PASS" in m[0]["content"] for m in model.messages)
        assert "ONTOLOGY phishing\n    definicao: Fraude por comunicação enganosa.\n    topico: golpes\n    tipo_de: fraude_online\n" in _syno(tmp_path)

    # --- integridade (avaliação de 2026-11-17) ---------------------------------

    def test_links_that_close_a_cycle_are_rejected_not_applied(self, tmp_path):
        """R1: parte do ciclo já existia; o erro cai num bloco não alterado."""
        syno = SYNO.replace("definicao: Fraude por comunicação enganosa.",
                            "definicao: Fraude por comunicação enganosa.\n    tipo_de: smishing")
        synp = _project(tmp_path, syno=syno)
        assert "SYNESIS_E118" not in _compile_codes(synp)
        first = {"new_parents": [], "top": [], "links": [
            {"child": "smishing", "parent": "vishing", "species": "KIND", "test": "um"},
            {"child": "vishing", "parent": "phishing", "species": "KIND", "test": "dois"}]}
        Model(first=first).run(synp, "taxonomia")
        assert "SYNESIS_E118" not in _compile_codes(synp), "the project must stay coherent"
        states = {k: r["state"] for k, r in _manifest(tmp_path, "taxonomia")["proposals"].items()}
        assert states["vishing KIND phishing"] == "rejected"
        assert states["smishing KIND vishing"] == "applied"

    def test_new_parent_receives_its_own_parent_in_the_second_pass(self, tmp_path):
        """R4: pai novo → pai existente, na segunda passada."""
        synp = _project(tmp_path)
        first = {"links": [], "top": [], "new_parents": [
            {"name": "ataque_mensagem", "children": ["smishing"], "species": "KIND",
             "justification": "ataque amplo", "questions": ["Q1"]}]}
        second = {"new_parents": [], "top": [], "links": [
            {"child": "ataque_mensagem", "parent": "fraude_online", "species": "KIND", "test": "todo ataque é fraude"}]}
        model = Model(first=first, second=second)
        model.run(synp, "taxonomia", options={"group_size": 3})
        assert any("SECOND PASS" in m[0]["content"] for m in model.messages)
        block = SynesisCompiler(synp).compile().linked_project.ontology_index["ataque_mensagem"]
        assert block.fields.get("tipo_de") == ["fraude_online"]
        assert "ataque_mensagem KIND fraude_online" in _manifest(tmp_path, "taxonomia")["proposals"]

    def test_new_parents_alone_trigger_the_second_pass(self, tmp_path):
        """R4: o material da segunda passada pode ser só de conceitos novos."""
        synp = _project(tmp_path)
        first = {"links": [], "top": [], "new_parents": [
            {"name": "ataque_mensagem", "children": ["smishing", "vishing"], "species": "KIND",
             "justification": "ataque amplo", "questions": ["Q1"]}]}
        model = Model(first=first)
        model.run(synp, "taxonomia", options={"group_size": 50})
        assert any("SECOND PASS" in m[0]["content"] for m in model.messages)

    def test_discarded_model_links_are_recorded(self, tmp_path):
        """R4: ligação que o código descarta não some sem registro."""
        synp = _project(tmp_path)
        first = {"new_parents": [], "top": [], "links": [
            {"child": "nao_existe", "parent": "phishing", "species": "KIND", "test": "x"},
            {"child": "smishing", "parent": "phishing", "species": "XYZ", "test": "x"}]}
        report = Model(first=first).run(synp, "taxonomia")
        notes = _manifest(tmp_path, "taxonomia").get("notes", {})
        reasons = " | ".join(notes.get("discarded", []))
        assert "unknown child" in reasons and "species outside the phase" in reasons
        assert "discarded before validation" in report

    def test_part_link_is_accepted(self, tmp_path):
        synp = _project(tmp_path)
        Model(first=FIRST).run(synp, "taxonomia")
        assert _manifest(tmp_path, "taxonomia")["proposals"]["url_maliciosa PART phishing"]["state"] == "applied"

    def test_mixed_phase_is_refused(self, tmp_path):
        phases = "\nPHASE ONTOLOGY taxonomia\n    FIELDS tipo_de, usado_por\nEND PHASE\n"
        with pytest.raises(ValueError, match="mixes hierarchy"):
            Model().run(_project(tmp_path, phases=phases), "taxonomia")


# --------------------------------------------------------------- sinônimos

class TestSynonyms:
    SYN = {"groups": [{"group": 0, "same_concept": True, "concept": "smishing",
                       "labels": [{"term": "smishings", "kind": "HIDDEN"}], "reason": "plural"}]}

    def _decision(self, synp, model_answer):
        # o número do grupo depende da ordem dos candidatos: descobre-o pela mensagem
        async def ask(client, messages, schema):
            line = next(l for l in messages[1]["content"].splitlines() if "smishings" in l)
            gid = int(line.split(":", 1)[0].split()[1])
            answer = json.loads(json.dumps(model_answer))
            for g in answer["groups"]:
                g["group"] = gid
            return answer
        with patch.object(synonym_mode, "ask_json", ask), \
             patch.object(phase_run, "LLMClient", side_effect=_client), \
             patch.object(synonym_mode, "warn_schema_fallbacks", MagicMock()):
            return phase_mode.run_phase(synp, "sinonimos", concurrent=1)

    def test_term_becomes_label_and_project_compiles_clean(self, tmp_path):
        synp = _project(tmp_path)
        report = self._decision(synp, self.SYN)
        assert "ONTOLOGY smishing\n    definicao: Phishing por SMS.\n    topico: golpes\n    nome_antigo: smishings\n" in _syno(tmp_path)
        assert (tmp_path / "a.syn").read_bytes() == ANNOTATIONS.encode("utf-8")
        codes = _compile_codes(synp)
        assert "SYNESIS_E114" not in codes and "SYNESIS_E115" not in codes  # rótulo ambíguo / conflito
        assert "smishing <- smishings (HIDDEN)" in report

    def test_duplicates_between_concepts_go_to_the_report_only(self, tmp_path):
        synp = _project(tmp_path)
        report = self._decision(synp, self.SYN)
        assert "pig-butchering × pig_butchering" in report or "pig_butchering × pig-butchering" in report
        assert _syno(tmp_path).count("ONTOLOGY pig") == 2
        assert ["pig-butchering", "pig_butchering"] in _manifest(tmp_path, "sinonimos")["notes"]["duplicates"]

    def test_label_equal_to_other_concept_is_refused(self, tmp_path):
        answer = {"groups": [{"group": 0, "same_concept": True, "concept": "smishing",
                              "labels": [{"term": "smishings", "kind": "ALTERNATIVE"}], "reason": ""}]}
        syno = SYNO + "\nONTOLOGY smishings\n    definicao: Outro.\nEND ONTOLOGY\n"
        synp = _project(tmp_path, syno=syno)
        # com bloco próprio, `smishings` é conceito: não é candidato a rótulo, é duplicata
        model = Model(synonyms=answer)
        report = model.run(synp, "sinonimos")
        assert "nome_antigo: smishings" not in _syno(tmp_path)
        assert "smishing × smishings" in report


def test_similarity_key_variants():
    assert similarity_key("pig-butchering") == similarity_key("Pig_Butchering")
    assert similarity_key("golpes_digitais") == similarity_key("golpe_digital")
    assert similarity_key("ações") == similarity_key("ação")


# ------------------------------------------------------------------ review

class TestReview:
    def test_sample_and_score_with_inclusion_weights(self, tmp_path):
        synp = _project(tmp_path)
        Model(first=FIRST).run(synp, "taxonomia")
        out = review_mode.sample(synp, "taxonomia", unit="edge", size=3, stratify="depth", seed=7)
        assert "File for judgement" in out
        path = tmp_path / "o.taxonomia.review_edge.csv"
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 3
        assert all(float(r["weight"]) >= 1 and 0 < float(r["inclusion_prob"]) <= 1 for r in rows)
        for i, r in enumerate(rows):
            r["judgement"] = "correct" if i else "wrong"
            r["direction_ok"] = "yes"
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=review_mode.COLUMNS)
            w.writeheader()
            w.writerows(rows)
        report = review_mode.score(path)
        assert "precision:" in report and "95% CI" in report
        assert "direction precision: 100.0%" in report
        assert "weighted by ITEMs reached" in report

    def _csv(self, path, rows):
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["stratum", "weight", "inclusion_prob", "items_reached",
                                               "unit", "judgement"])
            w.writeheader()
            w.writerows(rows)
        return path

    def test_one_judgement_does_not_produce_a_zero_width_interval(self, tmp_path):
        """R6: 1 julgado de 100 não é certeza."""
        path = self._csv(tmp_path / "s.csv", [dict(stratum="depth 0", weight="100", inclusion_prob="0.01",
                                                   items_reached="1", unit="edge", judgement="correct")])
        report = review_mode.score(path)
        assert "±0.0%" not in report
        assert "CI not reported" in report and "depth 0" in report

    def test_uniform_agreement_in_a_sample_gets_a_nonzero_interval(self, tmp_path):
        """R6: 5 de 5 corretos numa amostra de 50 → Agresti–Coull, largura > 0."""
        rows = [dict(stratum="depth 1", weight="10", inclusion_prob="0.1", items_reached="1",
                     unit="edge", judgement="correct") for _ in range(5)]
        report = review_mode.score(self._csv(tmp_path / "u.csv", rows))
        assert "±0.0%" not in report and "Agresti" in report

    def test_census_has_no_sampling_error_and_unjudged_strata_are_declared(self, tmp_path):
        rows = [dict(stratum="depth 1", weight="1", inclusion_prob="1", items_reached="1",
                     unit="edge", judgement=j) for j in ("correct", "wrong")]
        rows.append(dict(stratum="depth 2", weight="4", inclusion_prob="0.25", items_reached="1",
                         unit="edge", judgement=""))
        report = review_mode.score(self._csv(tmp_path / "c.csv", rows))
        assert "census strata" in report and "depth 1" in report
        assert "left out of the estimate: depth 2" in report

    def test_new_parent_without_question_has_its_own_stratum(self, tmp_path):
        first = {"links": [], "top": [], "new_parents": [
            {"name": "golpe_de_investimento", "species": "KIND", "children": ["pig_butchering"],
             "justification": "", "questions": []}]}
        synp = _project(tmp_path)
        Model(first=first).run(synp, "taxonomia")
        out = review_mode.sample(synp, "taxonomia", unit="concept", size=5, stratify="origin")
        assert "new parent without a question: 1 of 1" in out
        assert mf.APPLIED == "applied"


def test_hidden_relations_stay_out_of_the_prompt(tmp_path):
    """Piloto avaliado: os pares do gabarito (TIPO_DE) não entram no prompt."""
    template = TEMPLATE.replace(
        "    REQUIRED trecho, codigo\nEND ITEM FIELDS",
        "    REQUIRED trecho, codigo\n    OPTIONAL chain\nEND ITEM FIELDS",
    ) + (
        "\nFIELD chain TYPE CHAIN\n    SCOPE ITEM\n    ARITY >= 2\n    RELATIONS\n"
        "        TIPO_DE: A é tipo de B.\n        EXPLORA: A explora B.\n    END RELATIONS\nEND FIELD\n"
    )
    annotations = ANNOTATIONS.replace(
        "    codigo: smishing, url_maliciosa\n",
        "    codigo: smishing, url_maliciosa\n    chain: smishing -> TIPO_DE -> phishing\n",
    ).replace(
        "    codigo: vishing\n", "    codigo: vishing\n    chain: vishing -> EXPLORA -> fraude_online\n",
    )
    phases = PHASES.replace(
        "FIELDS tipo_de, parte_de, justificativa\n    INPUT definicao, topico",
        "FIELDS tipo_de, parte_de, justificativa\n    INPUT definicao, topico, chain")
    synp = _project(tmp_path, template=template, phases=phases)
    (tmp_path / "a.syn").write_bytes(annotations.encode("utf-8"))
    model = Model()
    model.run(synp, "taxonomia", options={"hide_relations": "TIPO_DE"})
    prompt = "\n".join(m[1]["content"] for m in model.messages)
    assert "vishing -> EXPLORA -> fraude_online" in prompt
    assert "TIPO_DE" not in prompt
    assert _manifest(tmp_path, "taxonomia")["config"]["policies"]["hide_relations"] == "TIPO_DE"
