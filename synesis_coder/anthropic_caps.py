"""anthropic_caps.py - Capacidades por modelo Claude: temperature, raciocínio e esforço.

Purpose:
    Os modelos Claude diferem no que aceitam, e o erro é sempre um HTTP 400 que
    derruba a chamada — no caminho JSON e no texto livre. Medido em 2026-10-09:
    Sonnet, Haiku e Opus 5.5 recusam `temperature`; recusam
    `thinking: {type: enabled, budget_tokens}`; e, SEM campo `thinking`,
    RACIOCINAM por padrão (adaptativo), consumindo o `max_tokens` da resposta.
    Desligar o raciocínio também depende do modelo: Haiku 5.5 aceita
    `disabled`, Sonnet 5.5 só `between_tools`, e Opus 5.5 não desliga.

    Prefixo de nome não basta para decidir; este módulo mantém uma TABELA de
    capacidades e a refina pela Models API quando ela responde
    (`capabilities.thinking.types`, `capabilities.effort`). A API não informa
    `temperature` nem `between_tools`: esses vêm só da tabela, e o `LLMClient`
    corrige um erro de tabela com UMA nova tentativa, apenas para o 400 de
    parâmetro não suportado (ver `unsupported_param`).

    Três controles, que a variável antiga misturava, ficam separados:
      - MODO: raciocinar ou não (`thinking` do chamador; `SYNESIS_CODER_THINKING_BUDGET` > 0 pede);
      - ESFORÇO: `SYNESIS_CODER_EFFORT` (low|medium|high|xhigh|max) → `output_config.effort`;
      - ORÇAMENTO: `SYNESIS_CODER_THINKING_BUDGET`, que só vale para extended thinking (`enabled`).
    Quando o modelo não suporta o que foi pedido, a troca é explícita e registrada
    (aviso único por modelo e rótulo no banner) — nunca silenciosa.

Fontes:
    https://platform.claude.com/docs/en/build-with-claude/thinking (tabela por modelo)
    chamadas diretas à API em 2026-10-09 (Sonnet/Haiku/Opus 5.5).

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Optional

_log = logging.getLogger(__name__)

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_RANK = {e: i for i, e in enumerate(EFFORTS)}

# Reserva de saída para o raciocínio quando ele roda sem orçamento declarado
# (adaptativo). Os tokens de raciocínio contam no max_tokens; sem reserva, a
# resposta é truncada e o caminho JSON cai para texto livre em silêncio.
_DEFAULT_THINKING_RESERVE = 8000


@dataclass(frozen=True)
class AnthropicCaps:
    """O que um modelo Claude aceita.

    Attributes:
        temperature: aceita o parâmetro `temperature`.
        thinking_default_on: SEM campo `thinking`, o modelo raciocina (adaptativo).
        thinking_types: tipos aceitos em `thinking.type`
            ("enabled", "adaptive", "disabled", "between_tools").
        off_type: como desligar o raciocínio quando ele é padrão
            ("disabled", "between_tools" ou None = não desliga). Irrelevante
            quando `thinking_default_on` é False (omitir já desliga).
        off_max_effort: maior esforço com que `off_type` é aceito (None = qualquer).
        effort: aceita `output_config.effort`.
        max_output: teto de saída do modelo (None = desconhecido).
        source: de onde vieram as capacidades ("tabela", "tabela+api", "padrão").
    """

    temperature: bool = True
    thinking_default_on: bool = False
    thinking_types: frozenset = field(default_factory=frozenset)
    off_type: Optional[str] = None
    off_max_effort: Optional[str] = None
    effort: bool = False
    max_output: Optional[int] = None
    source: str = "padrão"


def _caps(**kw) -> AnthropicCaps:
    kw["thinking_types"] = frozenset(kw.get("thinking_types", ()))
    return AnthropicCaps(source="tabela", **kw)


_GEN5_ADAPTIVE = dict(temperature=False, thinking_default_on=True, effort=True)

# Prefixo → capacidades. O prefixo MAIS LONGO vence (claude-opus-5-5 antes de claude-opus-5).
_TABLE: dict[str, AnthropicCaps] = {
    "claude-opus-5-5": _caps(
        thinking_types={"adaptive"}, off_type=None, max_output=128000, **_GEN5_ADAPTIVE
    ),
    "claude-sonnet-5-5": _caps(
        thinking_types={"adaptive", "between_tools"},
        off_type="between_tools",
        off_max_effort="high",
        max_output=128000,
        **_GEN5_ADAPTIVE,
    ),
    "claude-haiku-5-5": _caps(
        thinking_types={"adaptive", "disabled"},
        off_type="disabled",
        off_max_effort="high",
        **_GEN5_ADAPTIVE,
    ),
    "claude-fable-5": _caps(thinking_types={"adaptive"}, off_type=None, **_GEN5_ADAPTIVE),
    "claude-mythos-5": _caps(thinking_types={"adaptive"}, off_type=None, **_GEN5_ADAPTIVE),
    "claude-opus-5": _caps(
        thinking_types={"adaptive", "disabled"},
        off_type="disabled",
        off_max_effort="high",
        **_GEN5_ADAPTIVE,
    ),
    "claude-sonnet-5": _caps(
        thinking_types={"adaptive", "disabled"}, off_type="disabled", **_GEN5_ADAPTIVE
    ),
    "claude-mythos-preview": _caps(
        thinking_types={"adaptive", "enabled"}, off_type=None, **_GEN5_ADAPTIVE
    ),
    "claude-opus-4-8": _caps(
        temperature=False, thinking_types={"adaptive", "disabled"}, effort=True
    ),
    "claude-opus-4-7": _caps(
        temperature=False, thinking_types={"adaptive", "disabled"}, effort=True
    ),
    "claude-opus-4-6": _caps(thinking_types={"adaptive", "enabled", "disabled"}, effort=True),
    "claude-sonnet-4-6": _caps(thinking_types={"adaptive", "enabled", "disabled"}, effort=True),
    "claude-sonnet-4-5": _caps(thinking_types={"enabled", "disabled"}),
    "claude-3-7-sonnet": _caps(thinking_types={"enabled", "disabled"}),
}

_DEFAULT = AnthropicCaps()

_api_cache: dict[str, Optional[dict]] = {}
_api_lock = threading.Lock()
_warned: set[tuple[str, str]] = set()
_warn_lock = threading.Lock()


def table_caps(model: str) -> AnthropicCaps:
    """Capacidades pela tabela embutida (prefixo mais longo), ou o padrão conservador."""
    base = (model or "").split(":")[0].lower()
    for prefix in sorted(_TABLE, key=len, reverse=True):
        if base.startswith(prefix):
            return _TABLE[prefix]
    return _DEFAULT


def _models_api_capabilities(client, model: str) -> Optional[dict]:
    """`capabilities` da Models API, consultada uma vez por modelo. Falha → None.

    Metadado nunca é condição da execução: qualquer erro (rede, permissão,
    cliente simulado em teste) é engolido e a tabela segue valendo.
    """
    with _api_lock:
        if model in _api_cache:
            return _api_cache[model]
    caps = None
    try:
        info = client.models.retrieve(model)
        data = info.model_dump() if hasattr(info, "model_dump") else info
        if isinstance(data, dict) and isinstance(data.get("capabilities"), dict):
            caps = dict(data["capabilities"])
            if isinstance(data.get("max_tokens"), int):
                caps["_max_tokens"] = data["max_tokens"]
    except Exception as exc:  # noqa: BLE001 — metadado opcional
        _log.debug("Models API indisponível para %s (%s); usando a tabela", model, exc)
    with _api_lock:
        _api_cache[model] = caps
    return caps


def _supported(node) -> Optional[bool]:
    if isinstance(node, dict) and isinstance(node.get("supported"), bool):
        return bool(node["supported"])
    return None


def resolve_caps(model: str, client=None) -> AnthropicCaps:
    """Tabela, refinada pela Models API quando ela responde.

    A API informa quais `thinking.types` (adaptive/enabled/disabled) e esforços
    são aceitos. `temperature`, `between_tools` e o padrão ligado não vêm dela.
    Um modelo FORA da tabela que a API mostre como só-adaptativo é tratado como
    da geração 5 (raciocínio ligado por padrão, sem `temperature`); a nova
    tentativa do cliente corrige se a suposição errar.
    """
    caps = table_caps(model)
    api = _models_api_capabilities(client, model) if client is not None else None
    if not api:
        return caps
    raw_thinking = api.get("thinking")
    thinking: dict = raw_thinking if isinstance(raw_thinking, dict) else {}
    raw_types = thinking.get("types")
    types: dict = raw_types if isinstance(raw_types, dict) else {}
    found = {t: _supported(types.get(t)) for t in ("adaptive", "enabled", "disabled")}
    if not any(v is not None for v in found.values()):
        return caps
    new_types = set(caps.thinking_types)
    for t, ok in found.items():
        if ok is True:
            new_types.add(t)
        elif ok is False:
            new_types.discard(t)
    kw: dict = {"thinking_types": frozenset(new_types), "source": f"{caps.source}+api"}
    effort = _supported(api.get("effort"))
    if effort is not None:
        kw["effort"] = effort
    if isinstance(api.get("_max_tokens"), int):
        kw["max_output"] = api["_max_tokens"]
    if caps is _DEFAULT and found.get("enabled") is False and found.get("adaptive") is True:
        kw.update(
            temperature=False,
            thinking_default_on=True,
            off_type="disabled" if found.get("disabled") else None,
        )
    if caps.off_type and caps.off_type not in new_types:
        kw["off_type"] = None
    return replace(caps, **kw)


def requested_effort() -> Optional[str]:
    """`SYNESIS_CODER_EFFORT`, validado. Inválido → aviso e None."""
    v = os.environ.get("SYNESIS_CODER_EFFORT", "").strip().lower()
    if not v:
        return None
    if v not in EFFORTS:
        warn_once("*", f"SYNESIS_CODER_EFFORT={v!r} ignorado (use {', '.join(EFFORTS)})")
        return None
    return v


def thinking_reserve() -> int:
    """`SYNESIS_CODER_THINKING_RESERVE` (tokens), padrão 8000."""
    v = os.environ.get("SYNESIS_CODER_THINKING_RESERVE", "").strip()
    try:
        return max(0, int(v)) if v else _DEFAULT_THINKING_RESERVE
    except ValueError:
        return _DEFAULT_THINKING_RESERVE


def warn_once(model: str, message: str) -> None:
    key = (model, message)
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    _log.warning(message)


@dataclass(frozen=True)
class RequestPlan:
    """Parâmetros efetivamente enviados e o rótulo do que acontece com o raciocínio."""

    params: dict  # thinking / temperature / output_config.effort
    max_tokens: int
    thinking_label: str  # para banner e log: o que de fato vai rodar
    thinks: bool  # o modelo vai raciocinar nesta chamada


def plan_request(
    model: str,
    caps: AnthropicCaps,
    *,
    want_thinking: bool,
    budget: int,
    temperature: Optional[float],
    max_tokens: int,
    effort: Optional[str] = None,
    reserve: Optional[int] = None,
) -> RequestPlan:
    """Traduz o PEDIDO do chamador nos parâmetros que ESTE modelo aceita.

    Args:
        want_thinking: o chamador pediu raciocínio (thinking=True com orçamento > 0).
        budget: orçamento pedido; só usado com `enabled`.
        temperature: temperatura pedida (None → 0.0 quando aceita e sem raciocínio).
        max_tokens: limite da RESPOSTA; o raciocínio recebe reserva por cima.
        effort: esforço pedido (`SYNESIS_CODER_EFFORT`), já validado.
    """
    params: dict = {}
    reserve = thinking_reserve() if reserve is None else reserve
    if effort and not caps.effort:
        warn_once(
            model, f"SYNESIS_CODER_EFFORT={effort} ignorado: o modelo '{model}' não aceita esforço."
        )
        effort = None

    thinks = False
    out_tokens = max_tokens
    if want_thinking:
        if "enabled" in caps.thinking_types and budget > 0:
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
            out_tokens = max(max_tokens, budget + 4096)
            label = f"extended thinking (orçamento {budget})"
        elif "adaptive" in caps.thinking_types:
            params["thinking"] = {"type": "adaptive"}
            out_tokens = max_tokens + reserve
            label = "raciocínio adaptativo"
            if budget > 0:
                warn_once(
                    model,
                    f"SYNESIS_CODER_THINKING_BUDGET={budget}: o modelo '{model}' não aceita "
                    "orçamento (extended thinking); usando raciocínio adaptativo. "
                    "O esforço (SYNESIS_CODER_EFFORT) é o controle de profundidade.",
                )
        else:
            warn_once(
                model,
                f"SYNESIS_CODER_THINKING_BUDGET={budget} ignorado: modelo '{model}' "
                "não suporta raciocínio pelo coder.",
            )
            label = ""
        thinks = "thinking" in params
    elif caps.thinking_default_on:
        effort_ok = (
            caps.off_max_effort is None
            or effort is None
            or _EFFORT_RANK[effort] <= _EFFORT_RANK[caps.off_max_effort]
        )
        if caps.off_type and effort_ok:
            params["thinking"] = {"type": caps.off_type}
            label = f"raciocínio desativado ({caps.off_type})"
        else:
            # Não desliga: o raciocínio roda e recebe reserva de saída.
            thinks = True
            out_tokens = max_tokens + reserve
            if caps.off_type and not effort_ok:
                label = f"raciocínio adaptativo (não desliga com esforço {effort})"
            else:
                label = "raciocínio adaptativo (não desliga neste modelo)"
            warn_once(
                model,
                f"O modelo '{model}' raciocina por padrão e o pedido de desligar não "
                f"é aceito ({label}). Reserva de {reserve} tokens somada ao max_tokens; "
                "os tokens de raciocínio são cobrados como saída.",
            )
    else:
        label = ""

    if caps.temperature:
        params["temperature"] = 1.0 if thinks else (temperature if temperature is not None else 0.0)
    if effort:
        params["output_config"] = {"effort": effort}
    if caps.max_output:
        out_tokens = min(out_tokens, caps.max_output)
    return RequestPlan(params=params, max_tokens=out_tokens, thinking_label=label, thinks=thinks)


_TEMPERATURE_RE = re.compile(r"`?temperature`?\s+is\s+(deprecated|not supported)", re.I)
_THINKING_TYPE_RE = re.compile(r'"thinking\.type\.(\w+)"\s+is\s+not\s+supported', re.I)


def unsupported_param(exc: BaseException) -> Optional[tuple[str, str]]:
    """Reconhece SÓ o 400 de parâmetro não suportado. Outro erro → None.

    Returns:
        ("temperature", "") ou ("thinking_type", <tipo>), ou None.
    """
    if getattr(exc, "status_code", None) != 400:
        return None
    body = getattr(exc, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type") not in (None, "invalid_request_error"):
            return None
        msg = str((err or {}).get("message", "")) if isinstance(err, dict) else ""
    msg = msg or str(exc)
    if _TEMPERATURE_RE.search(msg):
        return ("temperature", "")
    m = _THINKING_TYPE_RE.search(msg)
    if m:
        return ("thinking_type", m.group(1).lower())
    return None


def without(caps: AnthropicCaps, fix: tuple[str, str]) -> AnthropicCaps:
    """Capacidades corrigidas depois de um 400 de parâmetro não suportado."""
    kind, value = fix
    if kind == "temperature":
        return replace(caps, temperature=False, source=f"{caps.source}+400")
    types = set(caps.thinking_types) - {value}
    off = None if caps.off_type == value else caps.off_type
    on = caps.thinking_default_on or value in ("disabled", "between_tools")
    return replace(
        caps,
        thinking_types=frozenset(types),
        off_type=off,
        thinking_default_on=on,
        source=f"{caps.source}+400",
    )
