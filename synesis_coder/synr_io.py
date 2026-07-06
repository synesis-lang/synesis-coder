"""Reader/writer para o formato .synr (Synesis Revision).

O formato .synr é sintaticamente idêntico ao .syn — todo conteúdo é válido
Synesis, com comentários qualificados (ignorados pelo compilador) como camada
de metadados de revisão.

Estrutura de um arquivo .synr:

    # $phase: critique
    # $model: claude-sonnet-4-6
    # $timestamp: 2026-04-24T14:23:01Z

    SOURCE @ref
        description: ...
    END SOURCE

    ITEM @ref
        text: ...
        chain: ...

        # REVISION
        # $suspicion_score: 0.84
        # $reason: wrong_direction
        # $chain: Trust -> INFLUENCES -> Community_Participation
    END ITEM

    ITEM @ref
        text: ...

        # REVISION
        # $suspicion_score: 0.18
        # $reason: none
    END ITEM
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --- Regex constants ---------------------------------------------------------

# Extrai "key" e "value" de uma linha do tipo: `# $key: value`
_TAG_RE = re.compile(r"^\s*#\s*\$([\w.]+):\s*(.+)$")

# Detecta a linha `# REVISION` (com ou sem espaços extras)
_REVISION_MARKER = re.compile(r"^\s*#\s*REVISION\s*$")

# Detecta início de bloco ITEM na coluna 0, capturando a bibref sem @
_ITEM_START = re.compile(r"^ITEM\s+@(\S+)")

# Detecta fim de bloco ITEM na coluna 0
_END_ITEM = re.compile(r"^END ITEM\s*$")

# Detecta início de qualquer bloco de nível raiz (para delimitar o header)
_ROOT_BLOCK = re.compile(r"^(SOURCE|ITEM|ONTOLOGY|PROJECT|TEMPLATE|END)\b")


# --- Data model --------------------------------------------------------------


@dataclass
class SynrDocument:
    """Representação de um arquivo .synr em memória.

    Attributes:
        header: Metadados do cabeçalho (phase, model, timestamp, ...).
        content: Conteúdo completo do arquivo (fonte de verdade para I/O).
        item_revisions: Lista de (bibref, tags) por bloco ITEM em ordem de
            aparecimento. tags é um dict {key: value} com os tags qualificados
            `# $key:` encontrados após `# REVISION` no bloco. Items sem
            `# REVISION` têm tags vazio.
    """

    header: dict[str, str] = field(default_factory=dict)
    content: str = ""
    item_revisions: list[tuple[str, dict[str, str]]] = field(default_factory=list)


# --- Public API --------------------------------------------------------------


def parse_synr(path: Path | str) -> SynrDocument:
    """Lê um arquivo .synr e retorna um SynrDocument estruturado.

    Args:
        path: Caminho para o arquivo .synr (ou .syn — o formato é idêntico).

    Returns:
        SynrDocument com header extraído, content e item_revisions.

    Raises:
        FileNotFoundError: Se o arquivo não existir.
    """
    content = Path(path).read_text(encoding="utf-8")
    header = _parse_header(content)
    item_revisions = _parse_item_revisions(content)
    return SynrDocument(header=header, content=content, item_revisions=item_revisions)


def write_synr(path: Path | str, doc: SynrDocument) -> None:
    """Escreve um SynrDocument em disco.

    Args:
        path: Caminho de destino (extensão .synr recomendada, não obrigatória).
        doc: Documento a escrever. doc.content é escrito diretamente.
    """
    Path(path).write_text(doc.content, encoding="utf-8")


def create_synr(
    syn_content: str,
    header: dict[str, str],
    item_revisions: list[Optional[dict[str, str]]],
    indent: str = "    ",
) -> SynrDocument:
    """Cria um SynrDocument a partir de conteúdo .syn + cabeçalho + revisões.

    Usada pelo modo `critique` para produzir um .synr a partir do output do LLM.

    Args:
        syn_content: Conteúdo do arquivo .syn de origem.
        header: Dict com 'phase', 'model', 'timestamp' (e quaisquer outros tags).
            As chaves 'phase', 'model', 'timestamp' são escritas primeiro.
        item_revisions: Lista de dicts de tags (ou None) por bloco ITEM em
            ordem de aparecimento. None significa "sem revisão para este ITEM".
            Dicts vazios também produzem nenhum bloco REVISION.
        indent: String de indentação usada nos blocos ITEM (padrão: 4 espaços).

    Returns:
        SynrDocument com content construído e item_revisions extraídos.
    """
    header_block = _build_header_block(header)
    injected = _inject_revisions(syn_content, item_revisions, indent=indent)
    content = (header_block + "\n\n" + injected) if header_block else injected

    parsed_revisions = _parse_item_revisions(content)
    return SynrDocument(header=header, content=content, item_revisions=parsed_revisions)


def extract_revision_tags(item_block: str) -> dict[str, str]:
    """Extrai tags qualificados do bloco REVISION dentro de um bloco ITEM.

    Encontra a linha `# REVISION` e coleta todas as linhas `# $key: value`
    consecutivas que a seguem. Para a coleta quando encontra uma linha não-tag
    (que não seja comentário vazio) ou o fim do texto.

    Args:
        item_block: Texto completo do bloco ITEM (de `ITEM @ref` até `END ITEM`).

    Returns:
        Dict {key: value} dos tags encontrados. Vazio se não houver `# REVISION`.
    """
    tags: dict[str, str] = {}
    in_revision = False

    for line in item_block.splitlines():
        stripped = line.strip()
        if _REVISION_MARKER.match(stripped):
            in_revision = True
            continue
        if in_revision:
            m = _TAG_RE.match(stripped)
            if m:
                tags[m.group(1)] = m.group(2).strip()
            elif stripped and not stripped.startswith("#"):
                # Linha não-comentário encerra o bloco REVISION
                in_revision = False

    return tags


def serialize_revision_block(
    tags: dict[str, str],
    indent: str = "    ",
) -> str:
    """Serializa um dict de tags para o formato de bloco `# REVISION`.

    Args:
        tags: Dict {key: value} com os tags a serializar.
        indent: Prefixo de indentação (padrão: 4 espaços, nível de campo ITEM).

    Returns:
        String multilinha com `# REVISION` seguido de `# $key: value` por tag.
        Retorna string vazia se tags for vazio.
    """
    if not tags:
        return ""
    lines = [f"{indent}# REVISION"]
    for key, value in tags.items():
        lines.append(f"{indent}# ${key}: {value}")
    return "\n".join(lines) + "\n"


# --- Internal helpers --------------------------------------------------------


def _build_header_block(header: dict[str, str]) -> str:
    """Serializa o cabeçalho em linhas `# $key: value`, com ordem garantida."""
    lines: list[str] = []
    # Ordem canônica para as 3 chaves principais
    for key in ("phase", "model", "timestamp"):
        if key in header:
            lines.append(f"# ${key}: {header[key]}")
    # Chaves extras em ordem de inserção
    for key, value in header.items():
        if key not in ("phase", "model", "timestamp"):
            lines.append(f"# ${key}: {value}")
    return "\n".join(lines)


def _parse_header(content: str) -> dict[str, str]:
    """Extrai os tags do cabeçalho (linhas `# $key: value` antes do 1º bloco raiz)."""
    header: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        # Parar ao encontrar qualquer bloco de nível raiz
        if _ROOT_BLOCK.match(stripped):
            break
        if not stripped or stripped.startswith("#"):
            m = _TAG_RE.match(stripped)
            if m:
                header[m.group(1)] = m.group(2).strip()
    return header


def _parse_item_revisions(content: str) -> list[tuple[str, dict[str, str]]]:
    """Itera os blocos ITEM em ordem e extrai tags de revisão de cada um."""
    revisions: list[tuple[str, dict[str, str]]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = _ITEM_START.match(lines[i])
        if m:
            bibref = m.group(1)
            item_lines = [lines[i]]
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i]):
                item_lines.append(lines[i])
                i += 1
            if i < len(lines):
                item_lines.append(lines[i])  # END ITEM
            tags = extract_revision_tags("\n".join(item_lines))
            revisions.append((bibref, tags))
        i += 1
    return revisions


def _inject_revisions(
    syn_content: str,
    revisions: list[Optional[dict[str, str]]],
    indent: str = "    ",
) -> str:
    """Injeta blocos `# REVISION` antes de `END ITEM` em cada bloco ITEM.

    Itera as linhas do conteúdo, rastreando qual bloco ITEM está ativo.
    Para cada END ITEM encontrado, insere o bloco de revisão correspondente
    (indexado pela ordem de aparecimento de blocos ITEM) se a revisão for
    não-None e não-vazia.

    Args:
        syn_content: Conteúdo do arquivo .syn.
        revisions: Lista de dicts (ou None) indexada pela ordem dos blocos ITEM.
            Se a lista for mais curta que o número de ITEMs, os ITEMs restantes
            não recebem injeção.
        indent: Indentação usada nos blocos REVISION.

    Returns:
        Conteúdo com blocos REVISION injetados.
    """
    lines = syn_content.splitlines(keepends=True)
    result: list[str] = []
    item_idx = -1
    in_item = False

    for line in lines:
        stripped = line.rstrip("\n").rstrip("\r")

        if not in_item and _ITEM_START.match(stripped):
            in_item = True
            item_idx += 1
            result.append(line)
            continue

        if in_item and _END_ITEM.match(stripped):
            # Injetar bloco de revisão antes de END ITEM
            rev = revisions[item_idx] if item_idx < len(revisions) else None
            if rev:
                block = serialize_revision_block(rev, indent=indent)
                if block:
                    # Garantir linha em branco antes do bloco se necessário
                    if result and result[-1].strip():
                        result.append("\n")
                    result.append(block)
            in_item = False
            result.append(line)
            continue

        result.append(line)

    return "".join(result)


# ---------------------------------------------------------------------------
# Escrita segura de output (R2 + R3 + R4 do plano Overwrite_e_Tolerancia)
# ---------------------------------------------------------------------------


def safe_write_output(
    output_path: Path,
    content: str,
    overwrite: bool = False,
    backup: bool = False,
) -> None:
    """Grava content em output_path com escrita atômica e proteção contra sobrescrita.

    Comportamento:
    - output_path não existe: grava diretamente (sem confirmação).
    - output_path existe + overwrite=True: grava (sem confirmação).
    - output_path existe + overwrite=False + TTY: pede confirmação ao usuário;
      nega → levanta FileExistsError.
    - output_path existe + overwrite=False + não-TTY: levanta FileExistsError
      (nunca bloqueia em CI/pipe).

    Escrita atômica: grava em arquivo temporário no mesmo diretório e usa
    os.replace() para renomear — garante que output_path nunca fica truncado
    em caso de interrupção (Ctrl-C, crash).

    Backup: se backup=True e output_path já existe, copia o conteúdo atual
    para output_path.with_suffix(output_path.suffix + ".bak") antes de gravar.

    Args:
        output_path: Caminho de destino (Path).
        content: Texto a gravar (UTF-8).
        overwrite: Se True, sobrescreve sem confirmação.
        backup: Se True, cria backup do arquivo existente antes de gravar.

    Raises:
        FileExistsError: Se output_path existe, overwrite=False e o usuário
            negou (TTY) ou o processo não é interativo (não-TTY).
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not overwrite:
        if sys.stdin.isatty() and sys.stderr.isatty():
            # Modo interativo: perguntar ao usuário
            sys.stderr.write(f"[PROMPT] {output_path.name} já existe. Sobrescrever? [y/N]: ")
            sys.stderr.flush()
            resp = sys.stdin.readline().strip().lower()
            confirmed = resp in ("s", "sim", "y", "yes")
            if not confirmed:
                raise FileExistsError(
                    f"Operação cancelada. '{output_path.name}' não foi modificado.\n"
                    "Use --overwrite para sobrescrever sem confirmação."
                )
        else:
            # Não-interativo (CI, pipe): abortar sem bloquear
            raise FileExistsError(
                f"'{output_path.name}' já existe. Execute novamente com --overwrite "
                "para sobrescrever (uso em scripts/CI requer a flag explícita)."
            )

    # Backup do arquivo existente antes de qualquer escrita
    if backup and output_path.exists():
        backup_path = output_path.with_suffix(output_path.suffix + ".bak")
        backup_path.write_bytes(output_path.read_bytes())

    # Escrita atômica: temp no mesmo diretório → os.replace()
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, str(output_path))
    except BaseException:
        # Interrupção (Ctrl-C, erro): remover temp, output_path permanece intacto
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
