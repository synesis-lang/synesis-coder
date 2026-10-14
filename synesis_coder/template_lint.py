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


def warn_template_issues(template_name: str, template_content: str, field_specs: Dict) -> None:
    """Emite avisos sobre o template, uma única vez por template e conteúdo."""
    key = template_name + ":" + hashlib.sha256(template_content.encode("utf-8")).hexdigest()
    if key in _already_warned:
        return
    _already_warned.add(key)

    altered = altered_guideline_lines(template_content, field_specs)
    if not altered:
        return
    details = "\n".join(f"  - {name}: {line}" for name, line in altered)
    logger.warning(
        "%s: %d linha(s) de GUIDELINES chegam ao modelo diferentes do que está "
        "escrito no template (o compilador anterior ao synesis 0.13.3 altera a "
        "palavra-chave no início da linha e listas numeradas). Atualize o synesis "
        "ou reescreva a linha sem começar por palavra-chave nem por '1.':\n%s",
        template_name, len(altered), details,
    )
