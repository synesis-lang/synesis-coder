"""model_facts.py - Metadados do motor LLM, uniformizados entre provedores.

Purpose:
    Um pesquisador rodou uma campanha inteira com o servidor entregando 4.096
    de 262.144 tokens de contexto — 1,5% da capacidade do modelo — sem que nada
    dissesse isso. O dado existia, a uma chamada HTTP de distância, e não era
    coletado.

    Este módulo busca esses metadados e os devolve num registro único, de modo
    que quem exibe não precise conhecer provedor nenhum. É um Anti-Corruption
    Layer: o formato de cada API morre aqui.

    Os cinco provedores diferem em host, caminho, método e nome de campo
    (`parameter_size` · `context_length` · `max_input_tokens` ·
    `inputTokenLimit`), e informam coisas diferentes — só o Ollama sabe
    parâmetros e quantização; a janela de contexto existe em 4 de 5. Campos
    ausentes ficam None e são OMITIDOS na exibição, nunca impressos como
    "desconhecido".

Princípio de segurança:
    Metadado é informação SOBRE a execução, nunca condição DELA. Toda falha de
    rede degrada para um registro mínimo (só o nome do modelo); nenhuma
    exceção escapa para o chamador.

Components:
    - ModelFacts: registro comum, todos os campos além do nome opcionais.
    - fetch_model_facts(client): despacha para o adaptador do backend.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Metadado nunca pode atrasar a campanha que descreve.
FETCH_TIMEOUT = 3.0


@dataclass(frozen=True)
class ModelFacts:
    """Fatos sobre o motor em uso, no que cada provedor souber informar.

    Attributes:
        display_name: Nome legível. Único campo sempre presente.
        context_window: Janela máxima do modelo, em tokens.
        context_served: Janela efetivamente servida nesta execução, quando
            difere do máximo. Só o Ollama distingue as duas: o servidor pode
            carregar o modelo com uma fração da capacidade.
        parameters: Contagem de parâmetros ("26.9B"). Só modelos locais.
        quantization: Nível de quantização ("IQ3_S"). Só modelos locais.
        host: Host do servidor, quando não é uma API hospedada.
        source: De onde os dados vieram, para o usuário julgar a procedência.
    """

    display_name: str
    context_window: Optional[int] = None
    context_served: Optional[int] = None
    parameters: Optional[str] = None
    quantization: Optional[str] = None
    host: Optional[str] = None
    source: Optional[str] = None

    @property
    def is_local(self) -> bool:
        """True quando o motor roda em servidor próprio (não API hospedada).

        Concorrência alta faz sentido contra API que atende em paralelo; contra
        uma GPU local, as chamadas disputam a mesma memória.
        """
        return self.host is not None

    @property
    def context_at_vendor_default(self) -> bool:
        """True quando a janela servida é o default de fábrica do servidor.

        Não se compara a janela servida com a do modelo por fração: quanto
        contexto cabe depende da VRAM da máquina, que o coder não conhece, e o
        KV cache cresce linearmente com o contexto disputando a mesma memória
        dos pesos. Servir bem menos que o máximo costuma ser dimensionamento
        CORRETO — alertar ali empurraria o pesquisador para uma configuração
        que vaza para a RAM e fica ordens de grandeza mais lenta.

        O que se pode afirmar sem conhecer o hardware é outra coisa: 4.096 é o
        default histórico do Ollama, herdado por quem nunca definiu
        `OLLAMA_CONTEXT_LENGTH`. Não é escolha, é ausência de escolha — e é
        pequeno demais para os prompts deste coder, que passam de 7.000 tokens
        só de template.
        """
        if not self.context_served or not self.context_window:
            return False
        # Só faz sentido chamar de "default" se o modelo comporta mais.
        return self.context_served == 4096 < self.context_window


def _http_json(url: str, *, data: Optional[dict] = None, headers: Optional[dict] = None) -> Optional[dict]:
    """GET/POST JSON com timeout curto. Devolve None em qualquer falha."""
    try:
        payload = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            parsed = json.load(resp)
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("Metadados indisponíveis em %s: %s", url, exc)
        return None


def short_name(model: str) -> str:
    """Nome legível a partir do ID cru.

    IDs de modelos locais carregam repositório, arquivo e quantização
    (`hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:Qwen3.8-...-IQ3_S`), ocupando a
    linha inteira sem informar mais que o nome.
    """
    if not isinstance(model, str):
        return str(model)
    name = model.rsplit(":", 1)[-1] if ":" in model else model
    return name.rsplit("/", 1)[-1]


def _suffix_key(info: dict, suffix: str) -> Optional[int]:
    """Valor da primeira chave terminada em `suffix`.

    No Ollama a chave de contexto é prefixada pela ARQUITETURA
    (`qwen35.context_length`, `llama.context_length`), que varia por modelo —
    ler por chave fixa funcionaria num modelo e falharia no seguinte.
    """
    for key, value in (info or {}).items():
        if key.endswith(suffix) and isinstance(value, int):
            return value
    return None


# ---------------------------------------------------------------------------
# Adaptadores por provedor — cada um devolve ModelFacts ou None
# ---------------------------------------------------------------------------


def _facts_ollama(model: str, api_url: str) -> Optional[ModelFacts]:
    """Ollama: o provedor mais completo — parâmetros, quantização e janela."""
    base = api_url.rstrip("/")
    data = _http_json(f"{base}/api/show", data={"model": model})
    if not data:
        return None

    details = data.get("details") or {}
    info = data.get("model_info") or {}
    host = urlparse(base).hostname

    served = None
    ps = _http_json(f"{base}/api/ps")
    for entry in (ps or {}).get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            ctx = entry.get("context_length")
            served = ctx if isinstance(ctx, int) else None
            break

    return ModelFacts(
        display_name=_ollama_display_name(model, info),
        context_window=_suffix_key(info, ".context_length"),
        context_served=served,
        parameters=details.get("parameter_size"),
        quantization=details.get("quantization_level"),
        host=host,
        source="Ollama",
    )


def _ollama_display_name(model: str, info: dict) -> str:
    """Nome do MODELO, não da tag.

    `short_name` corta o ID no `:`, o que serve para tags como
    `qwen3:27b` mas falha nos GGUF do Hugging Face, onde o que vem depois do
    `:` é a QUANTIZAÇÃO: `.../gemma-4-26B-A4B-it-GGUF:UD-Q3_K_XL` exibia
    "UD-Q3_K_XL" como se fosse o nome do modelo.

    Os metadados do GGUF trazem o nome real em `general.basename`; a tag só
    entra como último recurso.
    """
    for key in ("general.basename", "general.base_model.0.name"):
        value = (info or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return short_name(model)


def _facts_anthropic(model: str) -> Optional[ModelFacts]:
    """Anthropic: janela em `max_input_tokens`, nome em `display_name`."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    data = _http_json(
        f"https://api.anthropic.com/v1/models/{model}",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    if not data:
        return None
    window = data.get("max_input_tokens")
    return ModelFacts(
        display_name=data.get("display_name") or short_name(model),
        context_window=window if isinstance(window, int) else None,
        source="Anthropic",
    )


def _facts_openrouter(model: str, api_url: str) -> Optional[ModelFacts]:
    """OpenRouter: catálogo público, sem chave; janela em `context_length`."""
    data = _http_json(f"{api_url.rstrip('/')}/v1/models")
    if not data:
        return None
    for entry in data.get("data", []):
        if entry.get("id") != model:
            continue
        window = entry.get("context_length")
        return ModelFacts(
            display_name=entry.get("name") or short_name(model),
            context_window=window if isinstance(window, int) else None,
            source="OpenRouter",
        )
    return None


def _facts_gemini(model: str) -> Optional[ModelFacts]:
    """Gemini: a rota openai-compat NÃO informa a janela; só a nativa.

    Medido: `/v1beta/openai/models` devolve apenas id/object/owned_by/
    display_name. `inputTokenLimit` existe somente em `/v1beta/models/{m}`.
    """
    key = os.environ.get("SYNESIS_CODER_API_KEY")
    if not key:
        return None
    name = model.split("/")[-1]
    data = _http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{name}",
        headers={"x-goog-api-key": key},
    )
    if not data:
        return None
    window = data.get("inputTokenLimit")
    return ModelFacts(
        display_name=data.get("displayName") or short_name(model),
        context_window=window if isinstance(window, int) else None,
        source="Gemini",
    )


def _is_local_host(api_url: str) -> bool:
    """True para localhost ou IP de rede privada (RFC 1918)."""
    host = urlparse(api_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
    return False


def fetch_model_facts(llm_client) -> ModelFacts:
    """Metadados do motor deste cliente, com degradação garantida.

    Nunca levanta: qualquer falha devolve o registro mínimo (só o nome), de
    modo que um servidor de metadados indisponível jamais impeça a campanha.

    Args:
        llm_client: Cliente LLM com `.backend` e `.model`.

    Returns:
        ModelFacts — no mínimo com `display_name` preenchido.
    """
    model = getattr(llm_client, "model", "") or ""
    backend = (getattr(llm_client, "backend", "") or "").lower()
    minimal = ModelFacts(display_name=short_name(model))

    try:
        if backend == "anthropic":
            return _facts_anthropic(model) or minimal

        api_url = os.environ.get("SYNESIS_CODER_API_URL", "")
        if not api_url:
            return minimal

        if _is_local_host(api_url):
            return _facts_ollama(model, api_url) or ModelFacts(
                display_name=short_name(model),
                host=urlparse(api_url).hostname,
            )
        if "openrouter" in api_url:
            return _facts_openrouter(model, api_url) or minimal
        if "generativelanguage" in api_url:
            return _facts_gemini(model) or minimal
    except Exception as exc:  # defesa final: metadado nunca derruba campanha
        logger.debug("Falha ao obter metadados do modelo: %s", exc)

    return minimal
