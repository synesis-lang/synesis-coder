"""Identidade de conceito no coder = a do compilador (Spec ROLE/EXPORT §4)."""

import unicodedata

from synesis_coder.concept_index import _concept_key
from synesis_coder.identity import concept_key
from synesis_coder.syno_merge import concept_key as merge_key


def test_nfc_case_and_whitespace():
    nfd = unicodedata.normalize("NFD", "Inovação  Social")
    assert concept_key(nfd) == concept_key("inovação social")


def test_every_coder_path_agrees():
    nome = unicodedata.normalize("NFD", "  Ação Coletiva ")
    assert concept_key(nome) == _concept_key(nome) == merge_key(nome)


def test_separators_are_distinct():
    assert concept_key("pig_butchering") != concept_key("pig-butchering")
