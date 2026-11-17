"""
ontology_publish.py - Publicação segura de alterações num .syno

Propósito:
    Etapa A0a do plano ROLE/EXPORT (Spec §11, item 3): escolher um destino
    inequívoco, validar o CONJUNTO do projeto com as alterações em memória,
    rejeitar só os blocos que introduzem erro e conferir, antes de gravar, que
    as entradas lidas não mudaram (concorrência otimista).

Componentes principais:
    - resolve_destination: o .syno local em que a aplicação grava
    - InputSnapshot: hashes das entradas lidas no início
    - validate_changes: valida o projeto com o .syno de destino alterado e
      devolve os conceitos cujos blocos introduziram erro

Notas:
    Uma aplicação grava num único arquivo; a troca é atômica para ele
    (safe_write_output). Não há transação entre vários arquivos. Entre a última
    conferência dos hashes e a troca resta uma janela, declarada, sem bloqueio
    entre processos.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import synesis

from synesis_coder.project_loader import (
    _resolve_template_path,
    bibliography_kwargs,
    project_include_files,
)
from synesis_coder.syno_merge import block_line_spans


@dataclass
class Destination:
    """Onde a aplicação grava e como validar o conjunto."""

    path: Path
    key: str  # rótulo do arquivo para o compilador
    in_project: bool  # o .syno é um INCLUDE ONTOLOGY do projeto
    ontology_files: Dict[str, Path] = field(default_factory=dict)  # demais .syno do projeto


class DestinationError(ValueError):
    """Destino inadequado: SHARED, ambíguo, ou parte de uma pasta/padrão."""


def resolve_destination(project_path: Path, output_path: Optional[Path]) -> Destination:
    """O .syno de destino de uma aplicação.

    - Com `output_path`: esse arquivo. Se for um INCLUDE ONTOLOGY do projeto,
      precisa ser local (não SHARED); o conjunto é validado com os demais .syno.
      Se não for do projeto, a validação é só contra o template, como antes.
    - Sem `output_path`: o único .syno elegível do projeto (local e citado como
      arquivo); sem exatamente um, erro antes de qualquer chamada ao modelo.
    """
    includes = [i for i in project_include_files(project_path) if i["type"] == "ONTOLOGY"]
    by_path = {i["path"]: i for i in includes}

    chosen: Optional[Dict[str, Any]]
    if output_path is None:
        eligible = [i for i in includes if not i["shared"] and i["single"]]
        if len(eligible) != 1:
            names = ", ".join(i["key"] for i in eligible) or "none"
            raise DestinationError(
                f"The project has {len(eligible)} .syno file(s) eligible for writing ({names}). "
                "Set the destination with --output."
            )
        chosen = eligible[0]
        target = chosen["path"]
    else:
        target = Path(output_path).resolve()
        chosen = by_path.get(target)

    others = {i["key"]: i["path"] for i in includes if i["path"] != target}
    if chosen is None:
        return Destination(path=target, key=target.name, in_project=False, ontology_files=others)
    if chosen["shared"]:
        raise DestinationError(
            f"`{chosen['key']}` is included as INCLUDE SHARED: it is another project's "
            "organizing model and is not written by this run. Choose a local .syno."
        )
    return Destination(path=target, key=chosen["key"], in_project=True, ontology_files=others)


# ---------------------------------------------------------------------------
# Instantâneo das entradas
# ---------------------------------------------------------------------------


def _sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass
class InputSnapshot:
    """Hashes de tudo o que a geração e a validação usaram."""

    destination: Path
    hashes: Dict[Path, Optional[str]]

    @classmethod
    def take(cls, project_path: Path, destination: Destination) -> "InputSnapshot":
        project_path = Path(project_path).resolve()
        content = project_path.read_text(encoding="utf-8")
        paths: Set[Path] = {project_path, _resolve_template_path(content, project_path.parent).resolve()}
        paths.update(i["path"] for i in project_include_files(project_path))
        paths.add(destination.path)
        return cls(destination=destination.path, hashes={p: _sha(p) for p in sorted(paths)})

    def changed(self) -> Tuple[bool, List[Path]]:
        """(o destino mudou?, outras entradas que mudaram)."""
        changed = [p for p, h in self.hashes.items() if _sha(p) != h]
        return self.destination in changed, [p for p in changed if p != self.destination]


# ---------------------------------------------------------------------------
# Validação do conjunto
# ---------------------------------------------------------------------------


# Números de linha/coluna dentro da mensagem mudam com qualquer edição acima do
# bloco e não fazem parte da identidade do problema.
_POSITION_RE = re.compile(r"(?::\d+(?::\d+)?\b|\b(?:line|linha)\s+\d+)", re.IGNORECASE)


def _identity(error: Any) -> Tuple[str, str]:
    """Identidade estável de um diagnóstico: código e mensagem, sem posições."""
    return (str(getattr(error, "CODE", type(error).__name__)), _POSITION_RE.sub("", error.to_cli_line()))


def _error_entries(result: Any, key: str, text: str) -> List[Tuple[Tuple[str, str], str, str]]:
    """Erros do PROJETO INTEIRO: (identidade, bloco do destino onde caem ou "", mensagem).

    O bloco só é preenchido quando o erro cai no .syno de destino; um erro em
    outro arquivo, ou num bloco que não foi alterado, conta do mesmo jeito —
    o que importa é ser novo, não onde o compilador o localizou (ciclos são
    localizados num dos blocos do ciclo, que pode ser um preexistente).
    """
    spans = block_line_spans(text)
    out = []
    for error in result.validation_result.errors:
        concept = ""
        location = getattr(error, "location", None)
        if location is not None:
            file = Path(str(location.file)).as_posix()
            if file == key or file.endswith("/" + key) or Path(file).name == Path(key).name:
                concept = next((c for c, a, z in spans if a <= location.line <= z), "")
        out.append((_identity(error), concept, error.to_cli_line()))
    return out


def _new_errors(baseline: "Counter[Tuple[str, str]]", entries: list) -> list:
    """Entradas cuja identidade não existia antes (multiconjunto: uma a mais também é nova)."""
    seen: "Counter[Tuple[str, str]]" = Counter()
    fresh = []
    for entry in entries:
        seen[entry[0]] += 1
        if seen[entry[0]] > baseline.get(entry[0], 0):
            fresh.append(entry)
    return fresh


def _load(ctx: dict, destination: Destination, text: str, read: Callable[[Path], str]) -> Any:
    if not destination.in_project:
        return synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            ontology_contents={destination.key: text},
            dataset_index=ctx.get("dataset_index"),
        )
    ontology_contents = {k: read(p) for k, p in destination.ontology_files.items()}
    ontology_contents[destination.key] = text
    return synesis.load(
        project_content=ctx["project_content"],
        template_content=ctx["template_content"],
        annotation_contents=ctx.get("annotation_contents") or None,
        ontology_contents=ontology_contents,
        dataset_index=ctx.get("dataset_index"),
        **bibliography_kwargs(ctx),
    )


def validate_changes(
    ctx: dict,
    destination: Destination,
    original: str,
    build: Callable[[Set[str]], str],
    changed: Set[str],
    read: Callable[[Path], str] = lambda p: p.read_text(encoding="utf-8"),
    atomic: bool = False,
) -> Tuple[str, Dict[str, List[str]]]:
    """Valida o conjunto e rejeita os conceitos que introduzem erro.

    `atomic=True`: as alterações só fazem sentido juntas (consolidação: o
    preterido, o alvo e os aliases redirecionados). Não há aceitação parcial —
    um erro novo rejeita todas, e nada é aplicado.

    Critério (R1 da avaliação de 2026-11-17): compara os diagnósticos do
    **projeto inteiro** antes e depois, por identidade estável (código e
    mensagem, sem posições). Um erro que já existia não rejeita ninguém; um erro
    NOVO — em qualquer arquivo, localizado em qualquer bloco — tem de ser
    atribuído a uma alteração, e essa alteração sai.

    1. Erro novo localizado num bloco alterado: aquele conceito sai.
    2. Erro novo sem dono claro (ciclo localizado num bloco preexistente, erro
       em outro .syno): aceitação incremental, conceito a conceito em ordem
       determinística — fica o maior conjunto que não introduz erro novo.
    3. Conferência final: se ainda sobrar erro novo, nada é aplicado.

    Returns:
        (texto final, {conceito rejeitado: mensagens}).
    """
    baseline = Counter(e[0] for e in _error_entries(_load(ctx, destination, original, read), destination.key, original))
    accepted = set(changed)
    rejected: Dict[str, List[str]] = {}

    def fresh(keys: Set[str]) -> Tuple[str, list]:
        text = build(keys)
        return text, _new_errors(baseline, _error_entries(_load(ctx, destination, text, read), destination.key, text))

    if atomic:
        text, new = fresh(accepted)
        if not new:
            return text, rejected
        messages = [m for _i, _c, m in new]
        return build(set()), {concept: messages for concept in sorted(accepted)}

    # 1. Atribuição pela localização, enquanto ela resolver.
    for _ in range(len(changed) + 1):
        text, new = fresh(accepted)
        if not new:
            return text, rejected
        located = {c for _identity_, c, _msg in new if c in accepted}
        if not located:
            break
        for concept in sorted(located):
            accepted.discard(concept)
            rejected[concept] = [m for _i, c, m in new if c == concept]

    # 2. Sem dono pela localização: aceitação incremental.
    kept: Set[str] = set()
    for concept in sorted(accepted):
        _text, new = fresh(kept | {concept})
        if new:
            rejected[concept] = [m for _i, _c, m in new]
        else:
            kept.add(concept)

    # 3. Conferência final do conjunto mantido (a combinação também conta).
    text, new = fresh(kept)
    if new:
        for concept in kept:
            rejected[concept] = [m for _i, _c, m in new]
        return build(set()), rejected
    return text, rejected
