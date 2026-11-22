"""Verificação determinística de ancoragem: o trecho anotado existe na fonte?

Única classe de verificação da §5.3 do Estudo_Assimetria_Contexto_Critique que o
compilador NÃO cobre. Medido em 2026-08-18: `InvalidEnumeratedValue`,
`MissingRequiredField`, `MissingBundleField`, `BundleCountMismatch`,
`ScaleOutOfRange` e `InvalidChainRelation` já existem no compilador e já são
consumidos via `_validate_item_block`. Ancoragem de citação retornava 0 erros.

Por que determinístico e não LLM: comparar duas strings é exato e grátis.
Delegar isso ao crítico gasta uma chamada paga para responder pior.

O critério é o de `dashboard_davi_evidence_contract`: ancoragem **factual**, não
literalidade byte-a-byte. Aspas tipográficas, espaçamento e travessões variam
entre extrator e fonte sem que isso constitua defeito de anotação — por isso a
comparação usa a mesma normalização de `critique_mode._locate_excerpt`
(declarada em NORMALIZATION; `literal=True` compara sem normalizar).

Três estados por citação (correção D-2, 2026-11-20): `found`, `not_found` e
`unverifiable` (sem texto-fonte). Antes, uma referência sem abstract era pulada
e o relatório dizia "no problems"; só o primeiro QUOTATION preenchido e só a
primeira linha do valor eram lidos. Agora todo campo, toda ocorrência e toda
linha de continuação são verificados, e "verificado" exige zero `not_found` e
zero `unverifiable`.
"""

from __future__ import annotations

import re
from typing import Iterator, NamedTuple, Optional

from synesis_coder.modes.critique_mode import (
    _extract_abstract_from_bib,
    _locate_excerpt,
)

_ITEM_RE = re.compile(r"^ITEM\s+@(\S+)\s*$")
_END_ITEM_RE = re.compile(r"^END ITEM\s*$")


class AnchorIssue(NamedTuple):
    """Um ITEM cujo trecho não foi localizado na fonte."""

    bibref: str
    line: int
    excerpt: str

    def to_line(self) -> str:
        """Mensagem de uma linha, no estilo das diagnósticas da CLI."""
        shown = self.excerpt if len(self.excerpt) <= 60 else self.excerpt[:57] + "..."
        return (
            f"line {self.line}: ITEM @{self.bibref} — excerpt not found "
            f"in the source: {shown!r}"
        )


def _iter_items(content: str) -> Iterator[tuple[str, int, list[str]]]:
    """Percorre blocos ITEM, devolvendo (bibref, linha_inicial_1based, linhas)."""
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = _ITEM_RE.match(lines[i].rstrip("\r\n"))
        if m:
            start = i
            body: list[str] = []
            i += 1
            while i < len(lines) and not _END_ITEM_RE.match(lines[i].rstrip("\r\n")):
                body.append(lines[i])
                i += 1
            yield m.group(1), start + 1, body
        i += 1


def _field_values(body_lines: list[str], field: str) -> list[str]:
    """Todos os valores de um campo no corpo de um ITEM, com as linhas de continuação.

    Um campo pode aparecer mais de uma vez no ITEM (BUNDLE) e um valor pode
    seguir em linhas mais recuadas (`campo: valor` + continuação) ou começar na
    linha seguinte (`campo:` sozinho). Antes só a 1ª linha do 1º valor era lida.
    """
    pattern = re.compile(rf"^(\s*){re.escape(field)}\s*:\s*(.*)$", re.IGNORECASE)
    values: list[str] = []
    i = 0
    while i < len(body_lines):
        line = body_lines[i].rstrip("\r\n")
        m = pattern.match(line)
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        parts = [m.group(2).strip()] if m.group(2).strip() else []
        i += 1
        while i < len(body_lines):
            nxt = body_lines[i].rstrip("\r\n")
            if not nxt.strip():
                break
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            parts.append(nxt.strip())
            i += 1
        value = " ".join(parts).strip()
        if value:
            values.append(value)
    return values


def _field_value(body_lines: list[str], field: str) -> Optional[str]:
    """Primeiro valor de um campo (mantido para compatibilidade)."""
    values = _field_values(body_lines, field)
    return values[0] if values else None


def _first_value(body_lines: list[str], fields: list[str]) -> Optional[str]:
    for field in fields:
        value = _field_value(body_lines, field)
        if value:
            return value
    return None


def items_without_excerpt(content: str, fields: list[str]) -> int:
    """Quantos ITEMs não têm nenhum dos campos de trecho preenchido.

    Antes eles eram PULADOS em silêncio: com o campo errado (o padrão era
    `text`, e 25 templates usam `citation`/`trecho`), o relatório dizia "nenhum
    problema" sem ter verificado nada.
    """
    return sum(1 for _b, _l, body in _iter_items(content or "")
               if not _first_value(body, fields))


FOUND = "found"
NOT_FOUND = "not_found"
UNVERIFIABLE = "unverifiable"

NORMALIZATION = (
    "typographic quotes -> ASCII quotes",
    "en/em dashes -> hyphen",
    "LaTeX backslash escapes removed",
    "whitespace runs -> one space",
    "case-insensitive",
)


class QuotationResult(NamedTuple):
    """Resultado de uma citação: encontrada, não encontrada ou não verificável."""

    bibref: str
    line: int
    field: str
    occurrence: int
    excerpt: str
    status: str
    origin: Optional[str]

    def to_dict(self) -> dict:
        return self._asdict()


def _literal_contains(source: str, excerpt: str) -> bool:
    return bool(excerpt) and excerpt in source


def check_quotations(
    content: str,
    bib_content: str,
    fields: list[str],
    sources: Optional[dict[str, str]] = None,
    literal: bool = False,
) -> list[QuotationResult]:
    """Verifica TODAS as citações de cada ITEM, em três estados.

    - todos os campos de `fields` (os QUOTATION do template) e todas as
      ocorrências de cada um são verificados, não só o primeiro preenchido;
    - a fonte de um bibref vem de `sources` (texto de entrevista ou documento,
      o mesmo que o modelo leu) e, na falta dele, do abstract do `.bib`;
    - sem nenhuma fonte, a citação é `unverifiable` — nunca contada como ok.

    Args:
        content: Conteúdo de um arquivo .syn.
        bib_content: Conteúdo do .bib do projeto ("" se não houver).
        fields: Campos de trecho a verificar.
        sources: bibref -> texto-fonte, com prioridade sobre o abstract.
        literal: Se True, exige o trecho exatamente como está, sem normalização.
    """
    results: list[QuotationResult] = []
    cache: dict[str, tuple[Optional[str], Optional[str]]] = {}
    sources = sources or {}

    def source_of(bibref: str) -> tuple[Optional[str], Optional[str]]:
        if bibref not in cache:
            if bibref in sources:
                cache[bibref] = (sources[bibref], "source-dir")
            else:
                abstract = _extract_abstract_from_bib(bibref, bib_content) if bib_content else None
                cache[bibref] = (abstract, "bib-abstract" if abstract else None)
        return cache[bibref]

    for bibref, line_no, body in _iter_items(content or ""):
        for field in fields:
            for occurrence, excerpt in enumerate(_field_values(body, field)):
                text, origin = source_of(bibref)
                if not text:
                    status = UNVERIFIABLE
                elif literal:
                    status = FOUND if _literal_contains(text, excerpt) else NOT_FOUND
                else:
                    status = FOUND if _locate_excerpt(text, excerpt) is not None else NOT_FOUND
                results.append(QuotationResult(bibref, line_no, field, occurrence,
                                               excerpt, status, origin))
    return results


def summarize(results: list[QuotationResult]) -> dict:
    """Contagem por estado; `verified` só sem `not_found` e sem `unverifiable`."""
    counts = {s: sum(1 for r in results if r.status == s) for s in (FOUND, NOT_FOUND, UNVERIFIABLE)}
    counts["verified"] = bool(results) and counts[NOT_FOUND] == 0 and counts[UNVERIFIABLE] == 0
    return counts


def format_results(results: list[QuotationResult], total_items: int,
                   without_excerpt: int, fields: list[str], literal: bool = False) -> str:
    """Relatório da CLI: os três estados, a normalização usada e o veredito."""
    s = summarize(results)
    mode = "literal comparison" if literal else "after normalization: " + "; ".join(NORMALIZATION)
    lines = [
        f"Anchoring of {len(results)} quotation(s) in {total_items} ITEM(s) "
        f"[{', '.join(fields)}] — {mode}",
        f"  found: {s[FOUND]}   not found: {s[NOT_FOUND]}   unverifiable (no source text): {s[UNVERIFIABLE]}",
    ]
    for r in results:
        if r.status == FOUND:
            continue
        shown = r.excerpt if len(r.excerpt) <= 60 else r.excerpt[:57] + "..."
        label = "NOT FOUND" if r.status == NOT_FOUND else "UNVERIFIABLE"
        lines.append(f"  line {r.line}: {label:<12} ITEM @{r.bibref} [{r.field}] {shown!r}")
    if without_excerpt:
        lines.append(f"  ⚠ {without_excerpt} ITEM(s) without any quotation — not checked.")
    lines.append("Verified." if s["verified"] else "NOT verified.")
    return "\n".join(lines)


def check_anchoring(
    content: str,
    bib_content: str,
    field: str = "text",
    fields: Optional[list[str]] = None,
) -> list[AnchorIssue]:
    """Citações NÃO encontradas na fonte (API antiga, mantida).

    Agora verifica todos os campos e todas as ocorrências. Citações sem fonte
    (`unverifiable`) não entram aqui: use `check_quotations`, que as relata.
    """
    if not content or not bib_content:
        return []
    results = check_quotations(content, bib_content, fields or [field])
    return [AnchorIssue(bibref=r.bibref, line=r.line, excerpt=r.excerpt)
            for r in results if r.status == NOT_FOUND]


def format_report(
    issues: list[AnchorIssue], total_items: int,
    without_excerpt: int = 0, fields: Optional[list[str]] = None,
) -> str:
    """Formata o relatório de ancoragem para a CLI.

    `without_excerpt` ITEMs não tinham trecho nos `fields` e NÃO foram
    verificados: o relatório diz isso em vez de contá-los como ancorados.
    """
    aviso = ""
    if without_excerpt:
        nomes = ", ".join(fields or [])
        aviso = (f"\n  ⚠ {without_excerpt} of {total_items} ITEM(s) without an excerpt in "
                 f"[{nomes}] — not checked.")
    if not issues:
        verificados = total_items - without_excerpt
        return (f"Anchoring: {verificados} ITEM(s) checked, no problems."
                + aviso)

    rate = len(issues) / total_items if total_items else 0.0
    header = (
        f"Anchoring: {len(issues)} of {total_items} ITEM(s) "
        f"({rate:.1%}) with an excerpt not found in the source:"
    )
    body = "\n".join(f"  {i.to_line()}" for i in issues)
    return header + "\n" + body + aviso
