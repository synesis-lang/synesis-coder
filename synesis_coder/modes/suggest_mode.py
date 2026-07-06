"""Modo suggest: sugestão de códigos relevantes para um trecho de texto.

Diferente dos modos item/abstract/document/ontology, este modo não gera sintaxe
Synesis — retorna texto livre com sugestões de códigos existentes (ou novos) que
podem ser relevantes para o trecho fornecido.

Fluxo para projetos grandes (> 100 códigos):
    1. load_project(load_ontology=True) → ctx
    2. _select_topics_by_llm(ctx, text, llm_client) → List[str]  (passo 1)
    3. _build_enriched_code_list(ctx, topics) → str               (filtragem)
    4. build_suggest_prompt(ctx, text, enriched_codes) → messages (passo 2)
    5. LLMClient.call(messages) → raw_output
    6. _postprocess(raw_output, ctx) → saída final

Fluxo para projetos pequenos (≤ 100 códigos):
    1. load_project(load_ontology=True) → ctx
    2. build_suggest_prompt(ctx, text, enriched_codes) → messages (passo único)
    3. LLMClient.call(messages) → raw_output
    4. _postprocess(raw_output, ctx) → saída final
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Literal

from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_suggest_prompt, build_topic_filter_prompt
from synesis_coder.runtime_info import runtime_banner

_LARGE_PROJECT_THRESHOLD = 100
_MAX_CODES_PER_TOPIC = 25   # max códigos enviados no passo 2
_ONTOLOGY_DESC_MAX = 50     # chars da ontology_description a incluir


def process_suggest(
    project_path: Path,
    text: str,
    format: Literal["plain", "verbose"] = "plain",
    model: str | None = None,
) -> str:
    """Sugere códigos Synesis relevantes para um trecho de texto.

    Args:
        project_path: Caminho para o arquivo .synp.
        text: Trecho de texto a analisar.
        format: "plain" retorna apenas as sugestões;
                "verbose" inclui metadados (modelo, tópicos selecionados, etc.).
        model: ID do modelo LLM (sobrescreve SYNESIS_CODER_MODEL).

    Returns:
        String com as sugestões de códigos.
    """
    t0 = time.monotonic()

    ctx = load_project(project_path, load_annotations=True, load_ontology=True)
    client = LLMClient(model=model)
    runtime_banner(client, format=format)

    codes = ctx["code_index"]["codes"]
    is_large = len(codes) > _LARGE_PROJECT_THRESHOLD

    selected_topics: List[str] = []

    if is_large:
        # Passo 1: identificar tópicos relevantes (sem reasoning — tarefa trivial)
        selected_topics = _select_topics(ctx, text, client)

        # Passo 2: sugerir códigos dentro dos tópicos selecionados
        enriched = _build_enriched_code_list(ctx, selected_topics)
    else:
        # Passo único: todos os códigos com enriquecimento se disponível
        enriched = _build_enriched_code_list(ctx, [])

    messages = build_suggest_prompt(ctx, text, enriched)
    raw_output = client.call(messages, temperature=0.3, max_tokens=4096, thinking=False)
    suggestions = _postprocess(raw_output, ctx)

    elapsed = time.monotonic() - t0

    if format == "plain":
        return suggestions

    # verbose: adiciona cabeçalho com metadados
    project_name = ctx["project_path"].stem
    total_codes = len(codes)
    filter_info = (
        f"filtered to {_count_enriched(enriched)} via topics: {', '.join(selected_topics)}"
        if is_large and selected_topics
        else f"all {total_codes} codes (small project)"
    )
    header = (
        f"# synesis-coder suggest\n"
        f"# project: {project_name}\n"
        f"# model: {client.model}\n"
        f"# codes in project: {total_codes} ({filter_info})\n"
        f"# {client.usage.summary_line()}\n"
        f"# time: {elapsed:.1f}s\n"
    )
    return header + "\n" + suggestions


# ---------------------------------------------------------------------------
# Passo 1: identificação de tópicos via LLM
# ---------------------------------------------------------------------------


def _select_topics(ctx: dict, text: str, client: LLMClient) -> List[str]:
    """Usa o LLM para identificar 2-4 tópicos mais relevantes para o texto.

    Retorna lista de nomes de tópicos validados contra topic_index.
    Em caso de falha (LLM retorna nomes inválidos), retorna os top-5 tópicos
    por número de códigos como fallback.
    """
    available_topics = ctx["topic_index"]["topics"]
    if not available_topics:
        return []

    messages = build_topic_filter_prompt(available_topics, text)
    raw = client.call(messages, temperature=0.0, max_tokens=2048, thinking=False)

    # Validar: manter apenas tópicos que existem no projeto
    valid = set(available_topics)
    selected = []
    for line in raw.strip().splitlines():
        topic = line.strip().rstrip(",").strip()
        if topic in valid:
            selected.append(topic)

    if not selected:
        # Fallback: tópicos com mais códigos
        tm = ctx["topic_index"].get("topic_members", {})
        selected = sorted(tm.keys(), key=lambda t: -len(tm[t]))[:5]

    return selected[:5]  # no máximo 5 tópicos


# ---------------------------------------------------------------------------
# Construção da lista de códigos enriquecida com ontologia
# ---------------------------------------------------------------------------


def _build_enriched_code_list(ctx: dict, topics: List[str]) -> str:
    """Constrói string com códigos + frequência + descrição ontológica.

    Se `topics` não vazio, filtra por esses tópicos.
    Se vazio (projeto pequeno), usa todos os códigos.
    Ordena por frequência descendente e limita a _MAX_CODES_PER_TOPIC itens.
    """
    codes_all = ctx["code_index"]["codes"]
    stats = ctx["code_index"]["stats"]
    oi = ctx["ontology_index"]
    tm = ctx["topic_index"].get("topic_members", {})

    if topics:
        # Coletar códigos dos tópicos selecionados (preservando case do code_index)
        topic_codes_lower = set()
        for t in topics:
            for c in tm.get(t, []):
                topic_codes_lower.add(c.lower())
        candidates = [c for c in codes_all if c.lower() in topic_codes_lower]
    else:
        candidates = list(codes_all)

    # Ordenar por frequência e limitar
    candidates.sort(key=lambda c: -stats.get(c, 0))
    candidates = candidates[:_MAX_CODES_PER_TOPIC]

    lines = []
    for code in candidates:
        freq = stats.get(code, 0)
        # Buscar descrição na ontologia (case-insensitive)
        node = oi.get(code) or oi.get(code.lower())
        desc = ""
        if node:
            raw_desc = node.fields.get("ontology_description", "")
            if raw_desc:
                desc = " - " + str(raw_desc)[:_ONTOLOGY_DESC_MAX]

        lines.append(f"  {code} ({freq}){desc}")

    return "\n".join(lines)


def _count_enriched(enriched: str) -> int:
    """Conta o número de códigos na string enriquecida."""
    return sum(1 for line in enriched.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Pós-processamento: marcar [NEW] em códigos não encontrados no projeto
# ---------------------------------------------------------------------------


def _postprocess(raw_output: str, ctx: dict) -> str:
    """Verifica sugestões e marca [NEW] em códigos que não existem no projeto."""
    existing = {c.lower() for c in ctx["code_index"]["codes"]}
    lines = []
    for line in raw_output.strip().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("•") or stripped.startswith("-"):
            # Extrair nome do código: primeiro token antes de " - ", " (", ou espaço
            import re
            content = stripped.lstrip("•- ").strip()
            # Nome do código é a primeira palavra (pode conter _ mas não espaços)
            m = re.match(r"([A-Za-z][A-Za-z0-9_]*)", content)
            code_part = m.group(1) if m else ""

            # Verificar se já tem [NEW] ou se o código existe
            if code_part and "[NEW]" not in line:
                if code_part.lower() not in existing:
                    # Inserir [NEW] após o bullet
                    bullet = stripped[0]
                    indent = line[: len(line) - len(stripped)]
                    rest = stripped[1:].lstrip()
                    line = f"{indent}{bullet} [NEW] {rest}"

        lines.append(line)
    return "\n".join(lines)
