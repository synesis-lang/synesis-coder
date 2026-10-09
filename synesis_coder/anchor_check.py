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
comparação usa a mesma normalização de `critique_mode._locate_excerpt`.
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
            f"linha {self.line}: ITEM @{self.bibref} — trecho não localizado "
            f"na fonte: {shown!r}"
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


def _field_value(body_lines: list[str], field: str) -> Optional[str]:
    """Extrai o valor de um campo do corpo de um ITEM (primeira ocorrência)."""
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*(.*)$", re.IGNORECASE)
    for line in body_lines:
        m = pattern.match(line.rstrip("\r\n"))
        if m:
            return m.group(1).strip()
    return None


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


def check_anchoring(
    content: str,
    bib_content: str,
    field: str = "text",
    fields: Optional[list[str]] = None,
) -> list[AnchorIssue]:
    """Verifica que o trecho de cada ITEM ocorre no abstract do seu bibref.

    Só avalia ITEMs cujo bibref tem abstract disponível: sem fonte não há o que
    ancorar, e reportar seria ruído, não defeito.

    Args:
        content: Conteúdo de um arquivo .syn.
        bib_content: Conteúdo do .bib do projeto.
        field: Campo que carrega o trecho (usado quando `fields` é None).
        fields: Campos de trecho em ordem de preferência — os QUOTATION do
            template; o primeiro preenchido em cada ITEM é verificado.

    Returns:
        Lista de AnchorIssue, uma por ITEM não ancorado.
    """
    if not content or not bib_content:
        return []

    campos = fields or [field]
    issues: list[AnchorIssue] = []
    abstract_cache: dict[str, Optional[str]] = {}

    for bibref, line_no, body in _iter_items(content):
        excerpt = _first_value(body, campos)
        if not excerpt:
            continue

        if bibref not in abstract_cache:
            abstract_cache[bibref] = _extract_abstract_from_bib(bibref, bib_content)
        abstract = abstract_cache[bibref]
        if not abstract:
            continue  # sem fonte: nada a ancorar

        if _locate_excerpt(abstract, excerpt) is None:
            issues.append(AnchorIssue(bibref=bibref, line=line_no, excerpt=excerpt))

    return issues


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
        aviso = (f"\n  ⚠ {without_excerpt} de {total_items} ITEM(s) sem trecho em "
                 f"[{nomes}] — não verificados.")
    if not issues:
        verificados = total_items - without_excerpt
        return (f"Ancoragem: {verificados} ITEM(s) verificado(s), nenhum problema."
                + aviso)

    rate = len(issues) / total_items if total_items else 0.0
    header = (
        f"Ancoragem: {len(issues)} de {total_items} ITEM(s) "
        f"({rate:.1%}) com trecho não localizado na fonte:"
    )
    body = "\n".join(f"  {i.to_line()}" for i in issues)
    return header + "\n" + body + aviso
