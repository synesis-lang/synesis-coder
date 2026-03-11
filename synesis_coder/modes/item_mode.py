"""Modo item: geração síncrona de um bloco ITEM Synesis a partir de um texto.

Fluxo:
    1. load_project() → ctx
    2. build_item_prompt(ctx, bibref, text) → messages
    3. LLMClient.call(messages) → raw_syn
    4. validate_and_fix(raw_syn, ctx, llm_client) → (syn, ok)
    5. Retorna syn (plain) ou syn + log de validação (verbose)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_item_prompt
from synesis_coder.validator import validate_and_fix


def process_item(
    project_path: Path,
    bibref: str,
    text: str,
    format: Literal["plain", "verbose"] = "plain",
    model: str | None = None,
) -> str:
    """Gera um bloco ITEM Synesis a partir de um texto e bibref.

    Args:
        project_path: Caminho para o arquivo .synp.
        bibref: Referência bibliográfica (ex: "smith2024").
        text: Texto a ser codificado.
        format: "plain" retorna apenas o Synesis gerado;
                "verbose" inclui log de status e diagnósticos.
        model: ID do modelo LLM (sobrescreve env SYNESIS_CODER_MODEL).

    Returns:
        String com o output Synesis (e log se format="verbose").
    """
    # 1. Carregar contexto do projeto
    ctx = load_project(project_path, load_annotations=True)

    # 2. Construir prompt
    messages = build_item_prompt(ctx, bibref, text)

    # 3. Chamar LLM
    client = LLMClient(model=model)
    raw_syn = client.call(messages, temperature=0.0)

    # 4. Validar e corrigir
    final_syn, success = validate_and_fix(raw_syn, ctx, client)

    # 5. Formatar saída
    if format == "plain":
        return final_syn

    # verbose: inclui status
    status_line = "# OK" if success else "# AVISO: validação não concluída com sucesso"
    project_name = ctx["project_path"].stem
    header = (
        f"# synesis-coder item\n"
        f"# projeto: {project_name}\n"
        f"# bibref: @{bibref}\n"
        f"{status_line}\n"
    )
    return header + "\n" + final_syn
