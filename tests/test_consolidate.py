"""Etapa U: `synesis-coder consolidate` (Spec_USE_Etapa_U.md, §1 e §4).

Compilador, mesclagem, validação do conjunto e manifesto reais; sem modelo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from synesis.compiler import SynesisCompiler

from synesis_coder.modes.consolidate_mode import consolidate

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
    OPTIONAL tipo_de, usado_por, nota, usar
END ONTOLOGY FIELDS

FIELD definicao TYPE TEXT
    SCOPE ONTOLOGY
    ROLE NOTE DEFINITION
END FIELD

FIELD tipo_de TYPE CODE
    SCOPE ONTOLOGY
    ROLE HIERARCHY KIND
END FIELD

FIELD usado_por TYPE TEXT
    SCOPE ONTOLOGY
    ROLE LABEL ALTERNATIVE
END FIELD

FIELD nota TYPE TEXT
    SCOPE ONTOLOGY
END FIELD

FIELD usar TYPE CODE
    SCOPE ONTOLOGY
    ROLE USE
END FIELD
"""

BIB = "@article{a1, title={T}, author={A}, year={2024}}\n"

ANNOTATIONS = """\
SOURCE @a1
END SOURCE

ITEM @a1
    trecho: Golpe do abate de porcos.
    codigo: pig-butchering, golpe
END ITEM

ITEM @a1
    trecho: Investimento falso.
    codigo: pig_butchering, golpe_de_investimento
END ITEM
"""

SYNO = """\
# vocabulário curado
ONTOLOGY pig_butchering
    definicao: Golpe de investimento de longo prazo.
    tipo_de: golpe
END ONTOLOGY

ONTOLOGY pig-butchering
    definicao: Grafia com hífen.
    tipo_de: golpe_de_investimento, pig_butchering
    usado_por: abate de porcos
    nota: Registrado na primeira rodada.
END ONTOLOGY

ONTOLOGY golpe
    definicao: Fraude.
END ONTOLOGY

ONTOLOGY golpe_de_investimento
    definicao: Fraude com promessa de retorno.
    tipo_de: golpe
END ONTOLOGY

ONTOLOGY pig_butcher
    usar: pig-butchering
END ONTOLOGY
"""


def _project(root: Path, syno: str = SYNO, other: str | None = None) -> Path:
    for name, text in (("t.synt", TEMPLATE), ("r.bib", BIB), ("a.syn", ANNOTATIONS), ("o.syno", syno)):
        (root / name).write_bytes(text.encode("utf-8"))
    includes = 'INCLUDE ONTOLOGY "o.syno"\n'
    if other is not None:
        (root / "p.syno").write_bytes(other.encode("utf-8"))
        includes += 'INCLUDE ONTOLOGY "p.syno"\n'
    synp = root / "p.synp"
    synp.write_bytes(('PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
                      'INCLUDE ANNOTATIONS "a.syn"\n' + includes + "END PROJECT\n").encode("utf-8"))
    return synp


def _codes(synp: Path) -> list:
    vr = SynesisCompiler(synp).compile().validation_result
    return [e.CODE for e in vr.errors]


def _syno(root: Path) -> str:
    return (root / "o.syno").read_text(encoding="utf-8")


def test_consolidation_transfers_archives_and_redirects(tmp_path):
    synp = _project(tmp_path)
    report = consolidate(synp, "pig-butchering", "pig_butchering")
    text = _syno(tmp_path)
    # preterido só com USE; alvo com o que faltava; o alias do preterido redirecionado
    assert "ONTOLOGY pig-butchering\n    usar: pig_butchering\nEND ONTOLOGY" in text
    assert "ONTOLOGY pig_butcher\n    usar: pig_butchering\nEND ONTOLOGY" in text
    assert ("ONTOLOGY pig_butchering\n    definicao: Golpe de investimento de longo prazo.\n"
            "    tipo_de: golpe, golpe_de_investimento\n    usado_por: abate de porcos\n"
            "    nota: Registrado na primeira rodada.\nEND ONTOLOGY") in text
    assert text.startswith("# vocabulário curado\n")
    # o pai que seria o próprio alvo fica de fora; a definição do preterido é arquivada
    assert "self-reference" in report and "definicao" in report
    assert _codes(synp) == []
    assert (tmp_path / "a.syn").read_bytes() == ANNOTATIONS.encode("utf-8")
    data = json.loads((tmp_path / "o.consolidation.manifest.json").read_text(encoding="utf-8"))
    record = data["proposals"]["pig-butchering"]
    assert record["state"] == "applied"
    assert record["archived"]["definicao"] == "definicao: Grafia com hífen."


def test_revert_restores_both_blocks(tmp_path):
    synp = _project(tmp_path)
    consolidate(synp, "pig-butchering", "pig_butchering")
    consolidate(synp, "pig-butchering", revert=True)
    assert _syno(tmp_path) == SYNO
    data = json.loads((tmp_path / "o.consolidation.manifest.json").read_text(encoding="utf-8"))
    assert data["proposals"]["pig-butchering"]["state"] == "revertida"


def test_revert_after_target_edit_restores_only_the_alias(tmp_path):
    synp = _project(tmp_path)
    consolidate(synp, "pig-butchering", "pig_butchering")
    edited = _syno(tmp_path).replace("Golpe de investimento de longo prazo.", "Golpe de longo prazo.")
    (tmp_path / "o.syno").write_bytes(edited.encode("utf-8"))
    report = consolidate(synp, "pig-butchering", revert=True)
    assert "was edited" in report
    text = _syno(tmp_path)
    assert "    definicao: Grafia com hífen.\n" in text
    assert "Golpe de longo prazo." in text
    # o rótulo transferido fica só no alvo; o projeto continua válido
    assert text.count("usado_por: abate de porcos") == 1
    assert _codes(synp) == []


def test_dry_run_writes_nothing(tmp_path):
    synp = _project(tmp_path)
    report = consolidate(synp, "pig-butchering", "pig_butchering", dry_run=True)
    assert "dry run" in report
    assert _syno(tmp_path) == SYNO
    assert not (tmp_path / "o.consolidation.manifest.json").exists()


def test_alias_in_another_file_refuses_before_writing(tmp_path):
    """R2 (avaliação de 2026-11-17): o alias externo viraria E146; nada é gravado."""
    synp = _project(tmp_path, other="ONTOLOGY externo\n    usar: pig-butchering\nEND ONTOLOGY\n")
    assert _codes(synp) == []
    before = _syno(tmp_path)
    with pytest.raises(ValueError, match="target of aliases in other .syno files"):
        consolidate(synp, "pig-butchering", "pig_butchering")
    assert _syno(tmp_path) == before
    assert _codes(synp) == []


def test_blocks_in_different_files_are_refused(tmp_path):
    syno = SYNO.replace("ONTOLOGY golpe\n    definicao: Fraude.\nEND ONTOLOGY\n", "")
    synp = _project(tmp_path, syno=syno, other="ONTOLOGY golpe\n    definicao: Fraude.\nEND ONTOLOGY\n")
    with pytest.raises(ValueError, match="single file"):
        consolidate(synp, "pig-butchering", "golpe")


def test_template_without_use_role_is_refused(tmp_path):
    synp = _project(tmp_path)
    (tmp_path / "t.synt").write_bytes(TEMPLATE.replace("    ROLE USE\n", "").encode("utf-8"))
    with pytest.raises(ValueError, match="ROLE USE"):
        consolidate(synp, "pig-butchering", "pig_butchering")


def test_target_that_is_alias_is_refused(tmp_path):
    synp = _project(tmp_path)
    with pytest.raises(ValueError, match="is an alias"):
        consolidate(synp, "golpe", "pig_butcher")


def test_consolidating_a_parent_keeps_hierarchy_valid(tmp_path):
    # golpe_de_investimento tem filho (pig-butchering); consolidar no próprio filho formaria ciclo
    synp = _project(tmp_path)
    report = consolidate(synp, "golpe_de_investimento", "golpe")
    assert "ONTOLOGY golpe_de_investimento\n    usar: golpe\nEND ONTOLOGY" in _syno(tmp_path)
    assert "self-reference" in report  # tipo_de: golpe não vai para o próprio golpe
    assert _codes(synp) == []


def test_phases_skip_the_alias_after_consolidation(tmp_path):
    from synesis_coder.modes.ontology_mode import _generation_ctx, _plan_work
    from synesis_coder.modes.synonym_mode import candidate_groups
    from synesis_coder.project_loader import load_project
    from synesis_coder.syno_merge import existing_blocks
    from synesis_coder.vocabulary import build_table

    synp = _project(tmp_path)
    consolidate(synp, "pig-butchering", "pig_butchering")
    ctx = load_project(synp, load_annotations=True, load_ontology=False, tolerate_annotation_errors=True)
    text = _syno(tmp_path)
    table = build_table(ctx, {tmp_path / "o.syno": text})
    assert "pig-butchering" not in table and "pig_butcher" not in table
    groups = candidate_groups(table, [])
    assert not any(len(g["concepts"]) > 1 for g in groups)  # a duplicata saiu do relatório
    gen = _generation_ctx(ctx)
    plan, _other = _plan_work(["pig-butchering", "pig_butchering"], gen, existing_blocks(text), set(), True)
    assert "pig-butchering" not in plan  # o modo ontology não preenche o preterido


def test_preferred_label_of_alias_is_archived_not_transferred(tmp_path):
    template = TEMPLATE.replace(
        "    OPTIONAL tipo_de, usado_por, nota, usar\n", "    OPTIONAL tipo_de, usado_por, nota, usar, rotulo\n",
    ) + "\nFIELD rotulo TYPE TEXT\n    SCOPE ONTOLOGY\n    ROLE LABEL PREFERRED\nEND FIELD\n"
    syno = SYNO.replace("    usado_por: abate de porcos\n", "    usado_por: abate de porcos\n    rotulo: pig butchering com hífen\n")
    synp = _project(tmp_path, syno=syno)
    (tmp_path / "t.synt").write_bytes(template.encode("utf-8"))
    report = consolidate(synp, "pig-butchering", "pig_butchering")
    assert "rotulo: pig butchering com hífen" not in _syno(tmp_path)
    assert "rotulo" in report.split("archived")[1]
    assert _codes(synp) == []
