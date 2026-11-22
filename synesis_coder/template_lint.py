"""Avisos sobre o template: o que o modelo receberá diferente do que foi escrito.

Purpose:
    O coder obedece ao template, mas não o corrige. Quando um trecho escrito
    pelo autor chega ao modelo alterado, o resultado parece desobediência do
    modelo e nada avisa. Este módulo compara o texto bruto do `.synt` com o que
    o compilador entregou e aponta as diferenças, sem alterar nenhuma saída.

    Caso coberto: linha de GUIDELINES alterada pelo parser do compilador. Uma
    linha que começa com palavra-chave da linguagem chega em maiúsculas ou
    partida ("Include" -> "INCLUDE", "bundles" -> "BUNDLE s", "Topic" ->
    "TOPIC"), e uma lista numerada perde a forma ("1." -> "1 ."). Medido no
    template Social Acceptance: 4 linhas na v2.1 e 21 no template anterior.

Components:
    - altered_guideline_lines(): lista (campo, linha) que chegam alteradas.
    - warn_template_issues(): emite os avisos uma vez por template.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Um aviso por template e conteúdo: load_project roda a cada lote do abstract.
_already_warned: Set[str] = set()

_WS = re.compile(r"\s+")


def _norm(line: str) -> str:
    """Normaliza espaços: diferença só de espaçamento não é alteração."""
    return _WS.sub(" ", line).strip()


def altered_guideline_lines(template_content: str, field_specs: Dict) -> List[Tuple[str, str]]:
    """Linhas de GUIDELINES que o compilador entregou diferentes do `.synt`.

    Args:
        template_content: Texto bruto do template.
        field_specs: FieldSpec por nome, como devolvido pelo compilador.

    Returns:
        Lista de (nome do campo, linha como chegará ao modelo).
    """
    raw = {_norm(line) for line in template_content.splitlines()}
    altered: List[Tuple[str, str]] = []
    for name, spec in field_specs.items():
        for line in (getattr(spec, "guidelines", None) or "").splitlines():
            normalized = _norm(line)
            if normalized and normalized not in raw:
                altered.append((name, normalized))
    return altered


REQUIRE_GUIDELINES_ENV = "SYNESIS_CODER_REQUIRE_GUIDELINES"


def fields_without_guidelines(field_specs: Dict) -> List[Tuple[str, str, str]]:
    """Campos do template sem GUIDELINES: (nome, tipo, escopo), em ordem de nome.

    Sem GUIDELINES o coder manda ao modelo só a DESCRIPTION, e sem as duas, nada:
    o modelo improvisa a instrução, e o projeto compartilhado deixa de carregar
    o método daquele campo. Vale para todo escopo (SOURCE, ITEM, ONTOLOGY),
    inclusive campos preenchidos à mão, onde a GUIDELINE documenta o critério.
    """
    missing: List[Tuple[str, str, str]] = []
    for name in sorted(field_specs):
        spec = field_specs[name]
        if not (getattr(spec, "guidelines", None) or "").strip():
            ftype = getattr(getattr(spec, "type", None), "name", str(getattr(spec, "type", "")))
            scope = getattr(getattr(spec, "scope", None), "name", str(getattr(spec, "scope", "")))
            missing.append((name, ftype, scope))
    return missing


def guidelines_required() -> bool:
    """Exigência ligada por SYNESIS_CODER_REQUIRE_GUIDELINES (ou --require-guidelines)."""
    return os.environ.get(REQUIRE_GUIDELINES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def warn_template_issues(template_name: str, template_content: str, field_specs: Dict) -> None:
    """Emite avisos sobre o template, uma única vez por template e conteúdo.

    Com a exigência de GUIDELINES ligada, campo sem GUIDELINES é erro
    (ValueError) a cada carga, não só na primeira.
    """
    missing = fields_without_guidelines(field_specs)
    if missing and guidelines_required():
        listed = "\n".join(f"  - {name} ({ftype}, {scope})" for name, ftype, scope in missing)
        raise ValueError(
            f"{template_name}: {len(missing)} field(s) without GUIDELINES, and GUIDELINES "
            f"are required ({REQUIRE_GUIDELINES_ENV} / --require-guidelines):\n{listed}"
        )

    key = template_name + ":" + hashlib.sha256(template_content.encode("utf-8")).hexdigest()
    if key in _already_warned:
        return
    _already_warned.add(key)

    if missing:
        listed = "\n".join(f"  - {name} ({ftype}, {scope})" for name, ftype, scope in missing)
        logger.warning(
            "%s: %d field(s) without GUIDELINES. The model receives only the field's "
            "DESCRIPTION (or nothing) and improvises the instruction. Write GUIDELINES "
            "for every field, or require them with --require-guidelines:\n%s",
            template_name, len(missing), listed,
        )

    altered = altered_guideline_lines(template_content, field_specs)
    if not altered:
        return
    details = "\n".join(f"  - {name}: {line}" for name, line in altered)
    logger.warning(
        "%s: %d GUIDELINES line(s) reach the model differently from what is "
        "written in the template (the compiler before synesis 0.13.3 changes the "
        "keyword at the start of the line and numbered lists). Upgrade synesis "
        "or rewrite the line without starting with a keyword or with '1.':\n%s",
        template_name, len(altered), details,
    )
