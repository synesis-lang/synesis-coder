"""Mesclagem por bloco em .syno (etapa A0a do plano ROLE/EXPORT, Spec §11 item 3)."""

from __future__ import annotations

from synesis_coder.syno_merge import (
    block_line_spans,
    entries_by_field,
    existing_blocks,
    field_names,
    merge_into_syno,
    parse_syno,
)

SYNO = """\
# Ontologia do projeto — cabeçalho curado à mão

ONTOLOGY smishing
    # nota do pesquisador: não apagar
    definicao: Phishing por SMS.
    tipo_de: phishing
    exemplos:
        Mensagem com link falso.

        Mensagem com cobrança.
    topico: canais
END ONTOLOGY

# comentário entre blocos
ONTOLOGY vishing
    definicao: Phishing por voz.
END ONTOLOGY
"""

GENERATED = """\
ONTOLOGY smishing
    definicao: Fraude por mensagem de texto que induz a vítima a clicar.
    topico: fraude_movel
END ONTOLOGY
"""


def test_text_outside_changed_blocks_is_byte_identical():
    out = merge_into_syno(SYNO, {}, {})
    assert out == SYNO


def test_parse_keeps_every_byte():
    assert "".join(
        "".join(s.lines) if hasattr(s, "lines") else s for s in parse_syno(SYNO)
    ) == SYNO


def test_only_given_fields_are_replaced():
    out = merge_into_syno(SYNO, {"smishing": entries_by_field(GENERATED)}, {})
    block = out.split("ONTOLOGY smishing")[1].split("END ONTOLOGY")[0]
    assert "Fraude por mensagem de texto" in block
    assert "topico: fraude_movel" in block
    # preservados: comentário, campo CODE curado e campo de várias linhas
    assert "# nota do pesquisador: não apagar" in block
    assert "tipo_de: phishing" in block
    assert "Mensagem com link falso.\n\n        Mensagem com cobrança." in block
    # ordem preservada: definicao continua antes de tipo_de
    assert block.index("definicao:") < block.index("tipo_de:") < block.index("topico:")
    # o resto do arquivo não muda
    assert out.startswith("# Ontologia do projeto — cabeçalho curado à mão\n\n")
    assert "# comentário entre blocos\nONTOLOGY vishing\n    definicao: Phishing por voz.\nEND ONTOLOGY\n" in out


def test_missing_field_is_appended_before_end():
    out = merge_into_syno(SYNO, {"vishing": {"topico": ["    topico: canais\n"]}}, {})
    assert "ONTOLOGY vishing\n    definicao: Phishing por voz.\n    topico: canais\nEND ONTOLOGY" in out


def test_identity_ignores_case_and_spaces():
    out = merge_into_syno(SYNO, {"  VISHING ": {"topico": ["topico: canais\n"]}}, {})
    assert "    topico: canais\nEND ONTOLOGY" in out


def test_new_block_goes_to_the_end():
    new = "ONTOLOGY phishing\n    definicao: Fraude por comunicação enganosa.\nEND ONTOLOGY\n"
    out = merge_into_syno(SYNO, {}, {"phishing": new})
    assert out.startswith(SYNO.rstrip("\n"))
    assert out.endswith("\n\nONTOLOGY phishing\n    definicao: Fraude por comunicação enganosa.\nEND ONTOLOGY\n")


def test_new_blocks_into_empty_file():
    new = "ONTOLOGY a\n    definicao: x\nEND ONTOLOGY"
    assert merge_into_syno("", {}, {"a": new}) == new + "\n"


def test_crlf_is_preserved():
    crlf = SYNO.replace("\n", "\r\n")
    out = merge_into_syno(crlf, {"vishing": {"topico": ["    topico: canais\n"]}}, {})
    assert "\n" not in out.replace("\r\n", "")
    assert "    topico: canais\r\nEND ONTOLOGY" in out


def test_field_names_and_spans():
    blocks = existing_blocks(SYNO)
    assert field_names(blocks["smishing"]) == ["definicao", "tipo_de", "exemplos", "topico"]
    spans = block_line_spans(SYNO)
    assert spans == [("smishing", 3, 12), ("vishing", 15, 17)]
