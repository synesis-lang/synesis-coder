"""text_cleaner.py — Filtragem de ruído textual antes do chunking.

Saneamento e compressão de documentos textuais longos (currículos Lattes em
Markdown, relatórios, artigos) antes do envio ao LLM. Remove elementos que
consomem janela de contexto sem acrescentar valor à extração qualitativa.

Regras aplicadas (em ordem):
1. Seções vazias ou "não informado" (cabeçalho ATX sem conteúdo útil)
2. Boilerplate institucional (rodapés Lattes, endereços de CV, datas de geração)
3. Linhas de paginação e separadores visuais
4. Normalização de espaço em branco (espaços múltiplos, tabs, quebras excessivas)

Uso:
    from synesis_coder.text_cleaner import clean_document
    text = clean_document(raw_text)

O módulo é stateless e idempotente: aplicar clean_document duas vezes produz
o mesmo resultado que aplicar uma vez.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Padrões de boilerplate institucional (Lattes / CNPq)
# ---------------------------------------------------------------------------

_BOILERPLATE_PATTERNS: list[re.Pattern] = [
    # "Endereço para acessar este CV: http://lattes.cnpq.br/..."
    re.compile(r"(?im)^[ \t]*endere[çc]o para acessar este cv\s*:.*$"),
    # "Este currículo foi gerado pelo sistema Lattes em..."
    re.compile(r"(?im)^[ \t]*este curr[ií]culo foi gerado.*$"),
    # "Gerado em: DD/MM/AAAA HH:MM:SS" / "Relatório gerado em: ..."
    re.compile(r"(?im)^[ \t]*(?:relat[oó]rio\s+)?gerado\s+em\s*:\s*\d{2}/\d{2}/\d{4}.*$"),
    # "Atualização do CV: DD/MM/AAAA"
    re.compile(r"(?im)^[ \t]*atualiza[çc][aã]o\s+do\s+cv\s*:\s*\d{2}/\d{2}/\d{4}.*$"),
    # "Padrão CNPq" / "padrão cnpq"
    re.compile(r"(?im)^[ \t]*padr[aã]o\s+cnpq.*$"),
    # Links Lattes bare (sem título) — "[http://lattes...](...)"
    re.compile(r"\[https?://lattes\.cnpq\.br/\d+\]\(https?://lattes\.cnpq\.br/\d+\)"),
]

# ---------------------------------------------------------------------------
# Padrões de paginação
# ---------------------------------------------------------------------------

_PAGINATION_PATTERNS: list[re.Pattern] = [
    # "Página X de Y" / "página 3 de 12"
    re.compile(r"(?im)^[ \t]*p[aá]gina\s+\d+\s+de\s+\d+[ \t]*$"),
    # Linhas só com hífens/underscores/asteriscos (separadores visuais)
    re.compile(r"(?m)^[ \t]*[-_*]{4,}[ \t]*$"),
]

# ---------------------------------------------------------------------------
# Seções "vazias" — cabeçalho ATX seguido apenas de marcadores de ausência
# ---------------------------------------------------------------------------

# Strings que indicam ausência de conteúdo (case-insensitive, strip)
_EMPTY_SECTION_MARKERS = re.compile(
    r"^(?:n[aã]o\s+informado\.?|nenhum\s+item\s+cadastrado\.?|"
    r"sem\s+informa[çc][oõ]es\.?|n/a\.?|—+|-+)$",
    re.IGNORECASE,
)


def _remove_empty_sections(text: str) -> str:
    """Remove blocos ATX cujo conteúdo é apenas um marcador de ausência.

    Detecta padrão:
        ## Título da Seção
        <linha única com marcador de ausência>

    e remove ambas as linhas. Não toca em seções com conteúdo real.
    """
    lines = text.splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Cabeçalho ATX (# a ######)
        if re.match(r"^#{1,6}\s+\S", line):
            # Coletar linhas não-vazias após o cabeçalho
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            # Se a próxima linha não-vazia é um marcador de ausência, pular seção
            if j < len(lines) and _EMPTY_SECTION_MARKERS.match(lines[j].strip()):
                # Pular cabeçalho + linhas em branco + marcador
                i = j + 1
                # Pular trailing blanks desta seção
                while i < len(lines) and lines[i].strip() == "":
                    i += 1
                continue
        result.append(line)
        i += 1
    return "\n".join(result)


def _apply_patterns(text: str, patterns: list[re.Pattern]) -> str:
    for pat in patterns:
        text = pat.sub("", text)
    return text


def _normalize_whitespace(text: str) -> str:
    """Colapsa espaços/tabs múltiplos e normaliza quebras de linha."""
    # Múltiplos espaços/tabs dentro de linha → espaço único
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Mais de 2 quebras consecutivas → 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_document(text: str) -> str:
    """Aplica todas as regras de limpeza ao texto bruto.

    Args:
        text: Conteúdo bruto do documento (Markdown ou texto plano).

    Returns:
        Texto saneado, mais compacto, pronto para chunking.
    """
    text = _remove_empty_sections(text)
    text = _apply_patterns(text, _BOILERPLATE_PATTERNS)
    text = _apply_patterns(text, _PAGINATION_PATTERNS)
    text = _normalize_whitespace(text)
    return text
