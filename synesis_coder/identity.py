"""
identity.py - Identidade de conceito no synesis-coder

Propósito:
    Um único ponto para "dois nomes são o mesmo conceito?", delegado ao
    normalizador do compilador (`synesis.ast.normalize.normalize_code`), que
    é o único normalizador de identidade do ecossistema (Spec ROLE/EXPORT §4):
    NFC (quando o compilador já o aplica), espaços colapsados e caixa ignorada.
    `_`, `-` e espaço NÃO são equivalentes.

Notas:
    Antes, o coder tinha cópias próprias (`concept_index`, `ontology_mode`,
    `variant_candidates`); uma forma Unicode composta e outra decomposta do
    mesmo nome davam identidades diferentes no coder e iguais no compilador.
"""

from __future__ import annotations

import unicodedata

try:
    from synesis.ast.normalize import normalize_code as _compiler_normalize
except ImportError:  # pragma: no cover - compiladores antigos
    _compiler_normalize = None


def concept_key(name: object) -> str:
    """Identidade de um nome de conceito, como o compilador a calcula."""
    text = str(name)
    if _compiler_normalize is not None:
        # O compilador recente já aplica NFC; aplicar aqui também mantém a
        # mesma identidade com um compilador anterior à A2b.
        return str(_compiler_normalize(unicodedata.normalize("NFC", text)))
    return " ".join(unicodedata.normalize("NFC", text).strip().split()).lower()
