"""variant_candidates.py - Candidatos a variante de rótulo: DIAGNÓSTICO, nunca substituição.

Purpose:
    Rótulos como `questionário`/`questionários` ou `Decision-Making`/
    `Decision_Making` viram conceitos distintos. Uma regra lexical encontra
    esses pares, mas NÃO identifica só flexão: apaga acento e reduz sufixo, e
    considera iguais `pais`/`país`, `avó`/`avô`, `secretaria`/`secretária`,
    `economic`/`economics` (verificado em 2026-10-09). Mapear "só para um
    rótulo existente" não impede o erro: se `pais` existe e o modelo extrai
    corretamente `país`, a substituição troca o certo pelo errado.

    Por isso este módulo só APONTA candidatos. A única equivalência que a
    linguagem garante é a de `normalize_code` (caixa e espaços); hífen,
    sublinhado, acento e flexão dependem de interpretação, e na dúvida nada muda.

    Os candidatos vão para o inventário do `normalize` e para o log, para quem
    revisa os arquivos decidir — ou para um passo futuro, medido, que decida.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Iterable

# Contrações normalizadas para a preposição base; a posição na sequência é mantida.
_PREP = {
    "de": "de",
    "da": "de",
    "do": "de",
    "das": "de",
    "dos": "de",
    "em": "em",
    "no": "em",
    "na": "em",
    "nos": "em",
    "nas": "em",
    "a": "a",
    "ao": "a",
    "aos": "a",
    "à": "a",
    "às": "a",
    "por": "por",
    "pelo": "por",
    "pela": "por",
    "pelos": "por",
    "pelas": "por",
}
_ARTIGOS = {"o", "os", "as", "um", "uma", "uns", "umas", "the"}


def _sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _singular(t: str) -> str:
    for suf, rep in (
        ("oes", "ao"),
        ("aes", "ao"),
        ("ais", "al"),
        ("eis", "el"),
        ("ois", "ol"),
        ("ns", "m"),
        ("res", "r"),
        ("zes", "z"),
        ("ses", "s"),
    ):
        if t.endswith(suf) and len(t) > len(suf) + 2:
            return t[: -len(suf)] + rep
    if t.endswith("s") and len(t) > 3:
        return t[:-1]
    return t


def variant_key(label: str) -> tuple[str, ...]:
    """Chave que junta variantes de caixa, separador, acento, artigo, contração e número.

    Mesma ORDEM de palavras: reordenação (`controle_de_qualidade` ×
    `qualidade_do_controle`) muda o sentido e não entra.
    """
    partes = [p for p in re.split(r"[_\-\s]+", _sem_acento(str(label).lower())) if p]
    return tuple(_PREP.get(p, _singular(p)) for p in partes if p not in _ARTIGOS or p in _PREP)


def _contract_key(label: str) -> str:
    """O que a LINGUAGEM considera igual (`normalize_code`): caixa e espaços."""
    return " ".join(str(label).strip().split()).lower()


def candidate_groups(labels: Iterable[str]) -> list[list[str]]:
    """Grupos de rótulos que PODEM ser variantes (≥ 2 formas distintas pelo contrato).

    Rótulos que a linguagem já trata como iguais (só caixa/espaço) não são
    candidatos: para o compilador já são o mesmo conceito.
    """
    grupos: dict[tuple[str, ...], dict[str, str]] = defaultdict(dict)
    for label in labels:
        grupos[variant_key(label)].setdefault(_contract_key(label), label)
    return sorted(
        (sorted(formas.values()) for formas in grupos.values() if len(formas) > 1),
        key=lambda g: g[0].lower(),
    )
