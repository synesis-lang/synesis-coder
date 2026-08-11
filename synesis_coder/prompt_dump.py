"""Serialização do prompt montado em Markdown, sem chamar o LLM.

Purpose:
    Materializa o prompt que o coder ENVIARIA ao modelo, para revisão humana
    das GUIDELINES do template antes de gastar tokens. O arquivo resultante é
    autossuficiente: pode ser colado num chat, num harness de teste de prompt
    (promptfoo e afins), ou versionado junto a uma revisão do `.synt`.

    A montagem reusa as mesmas funções de `prompt_builder` que rodam em
    produção — não há reimplementação. O que este módulo faz é escolher qual
    `build_*` chamar (conforme modo e caminho ativo) e serializar o resultado.

Components:
    - resolve_path(): decide JSON vs texto-livre sem instanciar LLMClient.
    - dump_prompt(): monta as mensagens e devolve o Markdown.

Dependencies:
    - synesis_coder.prompt_builder: as funções de montagem de prompt.
    - synesis_coder.llm_client: apenas os helpers de introspecção de backend.
"""

from __future__ import annotations

from typing import List, Optional

_FENCE = "```"


def resolve_path() -> str:
    """Retorna "json" ou "text": o caminho que os modos usariam neste ambiente.

    Espelha `LLMClient.supports_json_schema()` SEM instanciar o cliente — o
    construtor exige ANTHROPIC_API_KEY, e um dump de prompt não deve depender
    de credencial para rodar. Ambos os helpers consultados são funções de
    módulo puras (env var e introspecção do SDK instalado).

    A duplicação da regra é deliberada e mínima: replicar duas condições aqui
    custa menos que expor uma credencial como pré-requisito de inspeção.
    """
    from synesis_coder.llm_client import (
        _anthropic_sdk_supports_output_config,
        _get_backend,
    )

    backend = _get_backend()
    if backend == "openai":
        return "json"
    if backend == "anthropic" and _anthropic_sdk_supports_output_config():
        return "json"
    return "text"


def _build_messages(
    ctx: dict,
    mode: str,
    path: str,
    bibref: Optional[str],
    text: Optional[str],
) -> List[dict]:
    """Chama a função de `prompt_builder` correspondente ao modo e caminho.

    Quando não há dado de entrada (sem bibref/text), usa um marcador visível
    no lugar do conteúdo dinâmico: o objetivo desse caso é revisar o system
    prompt, e um placeholder deixa claro que a seção USER é ilustrativa.
    """
    from synesis_coder import prompt_builder as pb

    ref = bibref or "BIBREF"
    body = text if text is not None else "<<texto da unidade de entrada>>"

    if mode == "item":
        if path == "json":
            return pb.build_item_values_prompt(ctx, ref, body)
        return pb.build_item_prompt(ctx, ref, body)

    if mode == "abstract":
        if path == "json":
            return pb.build_abstract_values_prompt(ctx, ref, body)
        return pb.build_abstract_prompt(ctx, ref, body)

    if mode == "document":
        if path == "json":
            return pb.build_document_values_prompt(ctx, ref, body, 0, 1)
        return pb.build_document_prompt(ctx, ref, body, 0, 1)

    if mode == "ontology":
        # O user message de ontology deriva de semantic_ctx (frequência,
        # relações e exemplos extraídos do corpus por código). Sem um código
        # escolhido não há contexto semântico real; um semantic_ctx vazio
        # produz a moldura da mensagem, e as GUIDELINES — que é o que se
        # revisa aqui — vivem integralmente no system prompt.
        code = text or "<<CÓDIGO>>"
        if path == "json":
            return pb.build_ontology_values_prompt(ctx, code, {})
        return pb.build_ontology_prompt(ctx, code, {})

    raise ValueError(f"Modo sem dump de prompt: {mode!r}")


def _fence_for(content: str) -> str:
    """Escolhe uma cerca maior que qualquer sequência de crases no conteúdo.

    GUIDELINES de template podem conter blocos de código; uma cerca de três
    crases fecharia cedo e quebraria o Markdown.
    """
    longest = 0
    run = 0
    for char in content:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def dump_prompt(
    ctx: dict,
    mode: str,
    bibref: Optional[str] = None,
    text: Optional[str] = None,
    path: Optional[str] = None,
) -> str:
    """Monta o prompt do modo e devolve o Markdown pronto para escrita.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        mode: "item" | "abstract" | "document" | "ontology".
        bibref: Referência bibliográfica, quando houver. Sem ela a seção USER
            sai com um marcador no lugar do valor.
        text: Conteúdo da unidade de entrada (texto, abstract, chunk) ou, no
            modo ontology, o nome do código.
        path: "json" | "text". None resolve pelo backend ativo.

    Returns:
        Documento Markdown com as seções SYSTEM e USER.
    """
    resolved_path = path or resolve_path()
    messages = _build_messages(ctx, mode, resolved_path, bibref, text)

    project_path = ctx["project_path"]
    path_label = (
        "JSON (structured outputs)" if resolved_path == "json" else "texto-livre"
    )

    lines: List[str] = [
        f"# Prompt — {project_path.stem} / {mode}",
        "",
        f"projeto: `{project_path.name}` · caminho: {path_label}",
    ]
    if bibref:
        lines.append(f"bibref: `@{bibref}`")
    lines.append("")

    for msg in messages:
        content = msg["content"]
        fence = _fence_for(content)
        heading = msg["role"].upper()
        cache_note = " (cacheável)" if msg.get("cache") else ""
        lines.extend([
            f"## {heading}{cache_note}",
            "",
            f"{fence}text",
            content,
            fence,
            "",
        ])

    return "\n".join(lines)
