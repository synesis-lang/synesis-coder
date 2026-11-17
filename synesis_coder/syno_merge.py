"""
syno_merge.py - Mesclagem por bloco em arquivos .syno

Propósito:
    Editar um `.syno` preservando estrutura: substituir ou acrescentar só alguns
    campos de um bloco ONTOLOGY, mantendo os demais campos, a ordem, os
    comentários e os campos de várias linhas; criar o bloco quando o conceito
    ainda não tem um. Etapa A0a do plano ROLE/EXPORT (Spec §11, item 3).

Componentes principais:
    - parse_syno: divide o texto em trechos (blocos ONTOLOGY e o que há entre eles)
    - block_entries: campos e comentários de um bloco, com suas linhas
    - merge_into_syno: aplica, de uma vez, as alterações de vários conceitos

Notas:
    Funciona sobre texto, linha a linha, sem passar pelo compilador: o compilador
    descarta comentários e não guarda a forma escrita. O resultado é sempre
    revalidado pelo compilador antes de ser gravado (ontology_mode).
    A identidade do conceito é a do compilador (`normalize_code`), quando
    disponível: espaços colapsados e caixa ignorada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from synesis_coder.identity import concept_key as _identity

_RE_BEGIN = re.compile(r"^(\s*)ONTOLOGY\s+(.+?)\s*(?:#.*)?$", re.IGNORECASE)
_RE_END = re.compile(r"^\s*END\s+ONTOLOGY\b", re.IGNORECASE)
_RE_FIELD = re.compile(r"^(\s*)([^\s:#][^:]*?)\s*:")


def concept_key(name: str) -> str:
    """Identidade de um conceito, como o compilador a calcula."""
    return _identity(name)


@dataclass
class Block:
    """Um bloco `ONTOLOGY <conceito> ... END ONTOLOGY`, com as linhas originais."""

    concept: str
    lines: List[str]  # da linha ONTOLOGY até END ONTOLOGY, inclusive

    @property
    def key(self) -> str:
        return concept_key(self.concept)


@dataclass
class Entry:
    """Um campo (`nome: valor` e suas linhas de continuação) ou um comentário."""

    name: Optional[str]  # None = comentário ou linha em branco dentro do bloco
    lines: List[str] = field(default_factory=list)


def parse_syno(text: str) -> List[object]:
    """Divide o texto em trechos: `Block` ou `str` (texto entre blocos, intacto)."""
    segments: List[object] = []
    lines = text.splitlines(keepends=True)
    buffer: List[str] = []
    i = 0
    while i < len(lines):
        match = _RE_BEGIN.match(lines[i].rstrip("\r\n"))
        if match:
            j = i + 1
            while j < len(lines) and not _RE_END.match(lines[j]):
                j += 1
            if j < len(lines):
                if buffer:
                    segments.append("".join(buffer))
                    buffer = []
                segments.append(Block(concept=match.group(2).strip(), lines=lines[i:j + 1]))
                i = j + 1
                continue
        buffer.append(lines[i])
        i += 1
    if buffer:
        segments.append("".join(buffer))
    return segments


def block_entries(block: Block) -> Tuple[str, List[Entry], str]:
    """Cabeçalho, entradas e rodapé de um bloco.

    Uma entrada de campo começa numa linha `nome:` com a indentação dos campos do
    bloco e leva as linhas seguintes mais indentadas (continuação ou valor de
    várias linhas). Linhas em branco entre campos e comentários viram entradas
    sem nome, na posição em que estavam.
    """
    header, body, footer = block.lines[0], block.lines[1:-1], block.lines[-1]
    field_indent: Optional[int] = None
    for line in body:
        m = _RE_FIELD.match(line)
        if m and line.strip() and not line.lstrip().startswith("#"):
            field_indent = len(m.group(1))
            break

    entries: List[Entry] = []
    for line in body:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        m = _RE_FIELD.match(line)
        is_field_start = (
            m is not None
            and not stripped.startswith("#")
            and field_indent is not None
            and indent == field_indent
        )
        if is_field_start and m is not None:
            entries.append(Entry(name=m.group(2).strip(), lines=[line]))
        elif entries and entries[-1].name is not None and (
            (stripped and indent > (field_indent or 0)) or not stripped
        ):
            # continuação do campo anterior (linhas em branco internas incluídas;
            # as finais são devolvidas como entradas soltas abaixo)
            entries[-1].lines.append(line)
        else:
            entries.append(Entry(name=None, lines=[line]))

    # Linhas em branco no fim de um campo não pertencem a ele.
    normalized: List[Entry] = []
    for entry in entries:
        if entry.name is not None:
            trailing: List[str] = []
            while len(entry.lines) > 1 and not entry.lines[-1].strip():
                trailing.insert(0, entry.lines.pop())
            normalized.append(entry)
            normalized.extend(Entry(name=None, lines=[t]) for t in trailing)
        else:
            normalized.append(entry)
    return header, normalized, footer


def field_names(block: Block) -> List[str]:
    """Nomes dos campos presentes no bloco, na ordem escrita."""
    return [e.name for e in block_entries(block)[1] if e.name is not None]


def entries_by_field(block_text: str) -> Dict[str, List[str]]:
    """Linhas de cada campo de um bloco gerado (texto de um único bloco)."""
    for segment in parse_syno(block_text):
        if isinstance(segment, Block):
            out: Dict[str, List[str]] = {}
            for entry in block_entries(segment)[1]:
                if entry.name is not None:
                    out.setdefault(entry.name, []).extend(entry.lines)
            return out
    return {}


def _reindent(lines: List[str], target: int) -> List[str]:
    """Desloca um campo para a indentação dos campos do bloco de destino."""
    if not lines:
        return lines
    current = len(lines[0]) - len(lines[0].lstrip())
    delta = target - current
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif delta >= 0:
            out.append(" " * delta + line)
        else:
            cut = min(-delta, len(line) - len(line.lstrip()))
            out.append(line[cut:])
    return out


def merge_block(block: Block, fields: Dict[str, List[str]], newline: str = "\n") -> Block:
    """Substitui ou acrescenta os campos dados, preservando o resto do bloco.

    Um campo presente é trocado no lugar da primeira ocorrência (ocorrências
    repetidas do mesmo campo saem, porque o valor gerado as substitui). Um campo
    ausente entra no fim do bloco, antes de `END ONTOLOGY`, na ordem recebida.
    """
    header, entries, footer = block_entries(block)
    indent = next(
        (len(e.lines[0]) - len(e.lines[0].lstrip()) for e in entries if e.name is not None),
        len(header) - len(header.lstrip()) + 4,
    )
    wanted = {name: _ensure_newlines(_reindent(lines, indent), newline) for name, lines in fields.items()}
    placed: set = set()
    out: List[str] = [header]
    for entry in entries:
        if entry.name is not None and entry.name in wanted:
            if entry.name not in placed:
                out.extend(wanted[entry.name])
                placed.add(entry.name)
            continue
        out.extend(entry.lines)
    # Novos campos depois do último campo, antes das linhas em branco finais.
    tail: List[str] = []
    while len(out) > 1 and not out[-1].strip():
        tail.insert(0, out.pop())
    for name, lines in wanted.items():
        if name not in placed:
            out.extend(lines)
    out.extend(tail)
    out.append(footer)
    return Block(concept=block.concept, lines=out)


def _ensure_newlines(lines: List[str], newline: str) -> List[str]:
    """Toda linha inserida termina com o fim de linha do arquivo de destino."""
    return [line.rstrip("\r\n") + newline for line in lines]


def merge_into_syno(
    text: str,
    changes: Dict[str, Dict[str, List[str]]],
    new_blocks: Dict[str, str],
) -> str:
    """Aplica as alterações de vários conceitos e cria os blocos novos.

    Args:
        text: conteúdo atual do .syno (pode ser vazio).
        changes: {conceito: {campo: linhas}} para conceitos com bloco existente.
        new_blocks: {conceito: texto do bloco} para conceitos sem bloco; entram
            no fim do arquivo, na ordem recebida.

    Conceitos de `changes` sem bloco no texto são ignorados (quem chama decide
    criá-los via `new_blocks`). Todo o texto fora dos blocos alterados volta
    byte a byte.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    keyed = {concept_key(k): v for k, v in changes.items()}
    segments = parse_syno(text)
    out: List[str] = []
    for segment in segments:
        if isinstance(segment, Block) and segment.key in keyed:
            out.append("".join(merge_block(segment, keyed[segment.key], newline).lines))
        elif isinstance(segment, Block):
            out.append("".join(segment.lines))
        else:
            out.append(str(segment))
    result = "".join(out)
    if new_blocks:
        pieces = [b.strip("\r\n").replace("\r\n", "\n").replace("\n", newline) for b in new_blocks.values()]
        body = (newline * 2).join(pieces) + newline
        # Uma linha em branco separa o conteúdo existente dos blocos novos.
        result = result.rstrip("\r\n") + newline * 2 + body if result.strip() else body
    return result


def replace_blocks(text: str, blocks: Dict[str, str]) -> str:
    """Troca blocos inteiros no lugar ({conceito: texto do bloco novo}).

    Usado pela consolidação (etapa U), que reescreve o preterido e o alvo. O
    texto fora desses blocos volta byte a byte; o bloco novo recebe o fim de
    linha do arquivo.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    keyed = {concept_key(k): v for k, v in blocks.items()}
    out: List[str] = []
    for segment in parse_syno(text):
        if isinstance(segment, Block) and segment.key in keyed:
            body = keyed[segment.key].strip("\r\n").replace("\r\n", "\n").replace("\n", newline)
            ending = segment.lines[-1][len(segment.lines[-1].rstrip("\r\n")):]
            out.append(body + ending)
        elif isinstance(segment, Block):
            out.append("".join(segment.lines))
        else:
            out.append(str(segment))
    return "".join(out)


def existing_blocks(text: str) -> Dict[str, Block]:
    """Blocos do texto por identidade de conceito (o primeiro, se repetido)."""
    out: Dict[str, Block] = {}
    for segment in parse_syno(text):
        if isinstance(segment, Block):
            out.setdefault(segment.key, segment)
    return out


def block_line_spans(text: str) -> List[Tuple[str, int, int]]:
    """(identidade, linha inicial, linha final), 1-based, de cada bloco do texto."""
    spans: List[Tuple[str, int, int]] = []
    line = 1
    for segment in parse_syno(text):
        if isinstance(segment, Block):
            spans.append((segment.key, line, line + len(segment.lines) - 1))
            line += len(segment.lines)
        else:
            line += str(segment).count("\n")
    return spans
