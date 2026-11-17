"""
manifest.py - Manifesto de execução de uma PHASE (etapa B2; Estudo ROLE/EXPORT §6.6)

Propósito:
    Registro de proveniência e de recuperação de uma fase que grava num .syno.
    Diz de que execução veio cada campo sem marcar a linguagem: o .syno fica
    limpo e o manifesto, ao lado dele, guarda entradas, configuração (sem
    segredos), propostas com estado e o motivo de parada.

Componentes principais:
    - Manifest: leitura, gravação atômica e transições de estado
    - recover: decide, ao iniciar, o que aconteceu com uma execução que parou
      no registro "aplicando"
    - sha_text / sha_file: hashes de antes e de depois

Ordem da gravação (estudo §6.6, "Recuperação"):
    1. o .syno final é serializado em memória e o hash de depois é calculado;
    2. o manifesto é gravado em estado "aplicando", atômico, com os dois hashes,
       as propostas e o texto a publicar;
    3. o .syno é trocado (temporário + os.replace);
    4. o manifesto é gravado como "aplicada", sem o texto.
    Ao iniciar, um "aplicando" é resolvido pelo hash do destino: o de depois
    completa o registro (aplicada), o de antes marca interrompida, outro é
    conflito. Nenhum caso chama o modelo de novo.

Estados de uma proposta: proposta, aplicada, rejeitada, interrompida, obsoleta
(e pendente, para conceito cujo bloco está em outro .syno do projeto).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_VERSION = 1

# Estados de execução (campo `status` do manifesto)
APPLYING = "applying"
APPLIED = "applied"
INTERRUPTED = "interrupted"
CONFLICT = "conflict"
NOTHING = "no_change"

# Estados de proposta
PROPOSED = "proposed"
REJECTED = "rejected"
OBSOLETE = "obsolete"
PENDING = "pending"

# Valores gravados por versões de teste anteriores (em português); lidos como os atuais.
_LEGACY_STATES = {"aplicando": APPLYING, "interrompida": INTERRUPTED, "conflito": CONFLICT,
                  "sem_mudanca": NOTHING, "obsoleta": OBSOLETE}
_LEGACY_NOTES = {"checagem_previa": "precheck", "criacao_de_pais": "parent_creation",
                 "agrupamento": "grouping", "topo": "top", "grupos": "groups",
                 "duplicatas": "duplicates", "termos_sem_conceito": "terms_without_concept"}


def _upgrade_proposals(proposals: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for key, record in proposals.items():
        if key.startswith("novo:"):
            key = "new:" + key[len("novo:"):]
        if isinstance(record, dict) and record.get("state") in _LEGACY_STATES:
            record = {**record, "state": _LEGACY_STATES[record["state"]]}
        out[key] = record
    return out


# Chaves de configuração que nunca vão para o manifesto.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def sha_text(text: str) -> str:
    """Hash dos bytes que serão gravados (UTF-8, fins de linha como compostos)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def without_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    """Remove qualquer chave que pareça segredo (api_key, token...)."""
    return {
        k: v for k, v in config.items()
        if not any(marker in k.upper() for marker in _SECRET_MARKERS)
    }


def manifest_path(destination: Path, phase_name: str) -> Path:
    """`<destino>.<fase>.manifest.json`, ao lado do .syno."""
    destination = Path(destination)
    return destination.with_name(f"{destination.stem}.{phase_name}.manifest.json")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Manifest:
    """Estado de uma fase sobre um destino."""

    path: Path
    phase: str
    destination: str
    status: str = ""
    started_at: str = ""
    finished_at: str = ""
    stop_reason: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Optional[str]] = field(default_factory=dict)  # caminho -> hash (antes)
    before: Optional[str] = None  # hash do destino antes
    after: Optional[str] = None  # hash do destino publicado
    proposals: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # conceito -> registro
    pending_text: Optional[str] = None  # só no estado "aplicando"
    # Decisões e registros da fase que não são propostas: conceitos julgados de
    # topo, checagem prévia, hashes por grupo, duplicatas para decisão.
    notes: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- disco

    @classmethod
    def load(cls, path: Path, phase: str, destination: Path) -> "Manifest":
        path = Path(path)
        if not path.is_file():
            return cls(path=path, phase=phase, destination=str(destination))
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            phase=data.get("phase", phase),
            destination=data.get("destination", str(destination)),
            status=_LEGACY_STATES.get(data.get("status", ""), data.get("status", "")),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            stop_reason=data.get("stop_reason", ""),
            config=data.get("config", {}),
            inputs=data.get("inputs", {}),
            before=data.get("before"),
            after=data.get("after"),
            proposals=_upgrade_proposals(data.get("proposals", {})),
            pending_text=data.get("pending_text"),
            notes={_LEGACY_NOTES.get(k, k): v for k, v in data.get("notes", {}).items()},
        )

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "phase": self.phase,
            "destination": self.destination,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_reason": self.stop_reason,
            "config": without_secrets(self.config),
            "inputs": dict(sorted(self.inputs.items())),
            "before": self.before,
            "after": self.after,
            "proposals": dict(sorted(self.proposals.items())),
        }
        if self.notes:
            data["notes"] = self.notes
        if self.pending_text is not None:
            data["pending_text"] = self.pending_text
        return data

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n")

    # ------------------------------------------------------------- consultas

    def same_run(self, inputs: Dict[str, Optional[str]], config: Dict[str, Any],
                 destination_hash: Optional[str]) -> bool:
        """A última execução publicada viu estas entradas e esta configuração?

        Compara o destino com o hash de DEPOIS (o próprio resultado não conta
        como mudança) e as demais entradas com o registro de antes.
        """
        if self.status != APPLIED or self.after is None or destination_hash != self.after:
            return False
        others = {k: v for k, v in inputs.items() if k != self.destination}
        recorded = {k: v for k, v in self.inputs.items() if k != self.destination}
        return others == recorded and without_secrets(config) == self.config

    def set_proposals(self, state: str, concepts: List[str], reason: str = "") -> None:
        for concept in concepts:
            record = self.proposals.setdefault(concept, {})
            record["state"] = state
            if reason:
                record["reason"] = reason
            else:
                record.pop("reason", None)


# ---------------------------------------------------------------------------
# Recuperação
# ---------------------------------------------------------------------------


class ManifestConflict(RuntimeError):
    """O destino não tem nem o hash de antes nem o de depois de um 'aplicando'."""


def recover(manifest: Manifest, destination: Path) -> Optional[str]:
    """Resolve um manifesto que parou em "aplicando". Devolve o estado decidido.

    - destino com o hash de depois: a troca aconteceu; completa como aplicada;
    - com o de antes: a troca não aconteceu; propostas interrompidas (o texto a
      publicar fica guardado, para retomar sem nova chamada ao modelo);
    - com outro: conflito; a fase para e nada é tocado.
    """
    if manifest.status != APPLYING:
        return None
    current = sha_file(destination)
    applying = [c for c, r in manifest.proposals.items() if r.get("state") == APPLYING]
    if current == manifest.after:
        manifest.status = APPLIED
        manifest.stop_reason = "recuperada: a troca tinha sido feita"
        manifest.finished_at = now()
        manifest.pending_text = None
        manifest.set_proposals(APPLIED, applying)
        manifest.save()
        return APPLIED
    if current == manifest.before:
        manifest.status = INTERRUPTED
        manifest.stop_reason = "interrupted before the .syno swap"
        manifest.finished_at = now()
        manifest.set_proposals(INTERRUPTED, applying)
        manifest.save()
        return INTERRUPTED
    raise ManifestConflict(
        f"{destination} matches neither the previous state nor the one published by the "
        f"run recorded in {manifest.path} (state 'applying'). Someone edited the "
        "file after an interrupted run. Check the file and, if it "
        "is correct, delete the manifest to start over."
    )
