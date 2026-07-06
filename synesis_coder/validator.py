"""Validação e correção de output Synesis via compilador.

O ciclo de correção usa temperature escalation para evitar loop
determinístico quando temperature=0:
    Tentativa 0: não aplicável (output já gerado)
    Tentativa 1 de correção: temperature=0.0
    Tentativa 2 de correção: temperature=0.2
    Tentativa 3 de correção: temperature=0.5
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

import synesis

if TYPE_CHECKING:
    from synesis_coder.debug_log import DebugRecorder
    from synesis_coder.llm_client import LLMClient

# Temperature escalation: cada elemento corresponde a uma tentativa de correção
CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]


def validate_and_fix(
    output: str,
    ctx: dict,
    llm_client: "LLMClient",
    annotation_key: str = "output.syn",
    max_tries: int = 3,
) -> Tuple[str, bool]:
    """Valida output via synesis.load(). Se inválido, solicita correção ao LLM.

    Ciclo:
        1. Valida output atual com synesis.load()
        2. Se válido → retorna (output, True)
        3. Se inválido → solicita correção com temperature escalada
        4. Repete até max_tries ou sucesso

    Warnings do compilador não bloqueiam quando o projeto não tem ONTOLOGY scope
    (projeto sem .syno definido), pois referências a ontologia são esperadas
    como ausentes nesse contexto.

    Args:
        output: Texto Synesis gerado pelo LLM.
        ctx: Contexto do projeto retornado por load_project().
        llm_client: Cliente LLM para solicitar correções.
        annotation_key: Nome virtual do arquivo para synesis.load().
        max_tries: Número máximo de tentativas de correção (padrão: 3).

    Returns:
        (output_final, success) — success=False se todas as tentativas falharam.
    """
    last_errors = ""
    output = _strip_markdown_fences(output)
    output = _extract_item_blocks(output)

    for attempt in range(max_tries + 1):
        try:
            result = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents={annotation_key: output},
                bibliography_content=ctx.get("bib_content"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                llm_client.fix(output, last_errors, temperature=temperature)
            )
            output = _extract_item_blocks(raw) or raw
            continue

        # Considerar válido se não há erros estruturais (ignorando OrphanItem,
        # que é esperado ao validar um ITEM isolado sem o .syn completo do projeto).
        if not _has_structural_errors(result):
            return output, True

        # Falhou — obter diagnósticos
        last_errors = result.get_diagnostics()

        # Última tentativa esgotada
        if attempt >= max_tries:
            break

        # Solicitar correção com temperature escalada
        temperature = CORRECTION_TEMPERATURES[
            min(attempt, len(CORRECTION_TEMPERATURES) - 1)
        ]
        raw = _strip_markdown_fences(
            llm_client.fix(output, last_errors, temperature=temperature)
        )
        output = _extract_item_blocks(raw) or raw

    # Todas as tentativas falharam
    error_header = (
        f"# ERRO: validação falhou após {max_tries} tentativa(s)\n"
        f"# Último diagnóstico:\n"
    )
    commented_errors = "\n".join(f"# {line}" for line in last_errors.splitlines())
    return error_header + commented_errors + "\n\n" + output, False


async def validate_and_fix_async(
    output: str,
    ctx: dict,
    llm_client: "LLMClient",
    annotation_key: str = "output.syn",
    max_tries: int = 3,
    recorder: "DebugRecorder | None" = None,
    context: tuple | None = None,
) -> Tuple[str, bool]:
    """Versão assíncrona de validate_and_fix() para modos de lote.

    Valida SOURCE + ITEM blocks via synesis.load(). Se inválido,
    solicita correção ao LLM usando fix_async().

    Args:
        output: Texto Synesis gerado pelo LLM (SOURCE + ITEMs).
        ctx: Contexto do projeto retornado por load_project().
        llm_client: Cliente LLM para solicitar correções.
        annotation_key: Nome virtual do arquivo para synesis.load().
        max_tries: Número máximo de tentativas de correção.
        recorder: DebugRecorder opcional (flag --debug). None = sem overhead.

    Returns:
        (output_final, success) — success=False se todas as tentativas falharam.
    """
    last_errors = ""
    output = _strip_markdown_fences(output)
    extracted = _extract_annotation_blocks(output)
    if extracted:
        output = extracted

    for attempt in range(max_tries + 1):
        try:
            result = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents={annotation_key: output},
                bibliography_content=ctx.get("bib_content"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if recorder is not None:
                recorder.record_validation(
                    attempt=attempt, submitted=output, success=False,
                    diagnostics=[], raw_diagnostic=last_errors, context=context,
                )
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                await llm_client.fix_async(
                    output, last_errors, temperature=temperature, context=context
                )
            )
            extracted = _extract_annotation_blocks(raw)
            output = extracted or raw
            continue

        if not _has_structural_errors(result):
            if recorder is not None:
                recorder.record_validation(
                    attempt=attempt, submitted=output, success=True,
                    diagnostics=[], context=context,
                )
            return output, True

        last_errors = result.get_diagnostics()
        if recorder is not None:
            from synesis_coder.debug_log import translate_diagnostics
            recorder.record_validation(
                attempt=attempt, submitted=output, success=False,
                diagnostics=translate_diagnostics(result), raw_diagnostic=last_errors,
                context=context,
            )

        if attempt >= max_tries:
            break

        temperature = CORRECTION_TEMPERATURES[
            min(attempt, len(CORRECTION_TEMPERATURES) - 1)
        ]
        raw = _strip_markdown_fences(
            await llm_client.fix_async(
                output, last_errors, temperature=temperature, context=context
            )
        )
        extracted = _extract_annotation_blocks(raw)
        output = extracted or raw

    error_header = (
        f"# ERRO: validação falhou após {max_tries} tentativa(s)\n"
        f"# Último diagnóstico:\n"
    )
    commented_errors = "\n".join(f"# {line}" for line in last_errors.splitlines())
    return error_header + commented_errors + "\n\n" + output, False


def _has_structural_errors(result) -> bool:
    """Retorna True se houver erros estruturais (excluindo OrphanItem).

    OrphanItem (ITEM sem SOURCE correspondente) é esperado ao validar um ITEM
    isolado no modo item — o SOURCE existe nas anotações do projeto mas não é
    carregado para evitar estourar o TPM da API.

    Outros erros de validação (sintaxe, campos inválidos, relações inválidas)
    são estruturais e impedem o uso do output.
    """
    try:
        from synesis.ast.results import OrphanItem
    except ImportError:
        # Se OrphanItem não existe nesta versão do compilador, usar has_errors()
        return result.has_errors()

    structural_errors = [
        err for err in result.validation_result.errors
        if not isinstance(err, OrphanItem)
    ]
    return len(structural_errors) > 0


def _extract_item_blocks(text: str) -> str:
    """Extrai apenas os blocos ITEM...END ITEM do output do LLM.

    O LLM às vezes gera blocos ONTOLOGY, SOURCE ou outros junto com os ITEMs.
    Esta função descarta tudo que não seja ITEM, preservando apenas o conteúdo
    que o modo item deve produzir.

    Retorna string com os blocos ITEM concatenados, ou string vazia se nenhum
    bloco ITEM for encontrado (o chamador deve usar o output original nesse caso).
    """
    import re

    pattern = re.compile(
        r"^ITEM\s+@\S+.*?^END ITEM",
        re.MULTILINE | re.DOTALL,
    )
    blocks = pattern.findall(text)
    if not blocks:
        return ""
    return "\n\n".join(block.strip() for block in blocks)


def _extract_annotation_blocks(text: str) -> str:
    """Extrai blocos SOURCE e ITEM do output do LLM para o modo abstract.

    Preserva SOURCE...END SOURCE e ITEM...END ITEM, descartando
    ONTOLOGY, PROJECT, TEMPLATE ou qualquer outro tipo de bloco.

    Retorna string com os blocos concatenados, ou string vazia se nenhum
    bloco for encontrado.
    """
    import re

    pattern = re.compile(
        r"^(?:SOURCE|ITEM)\s+@\S+.*?^END (?:SOURCE|ITEM)",
        re.MULTILINE | re.DOTALL,
    )
    blocks = pattern.findall(text)
    if not blocks:
        return ""
    return "\n\n".join(block.strip() for block in blocks)


def validate_ontology_entry(
    output: str,
    ctx: dict,
    llm_client: "LLMClient",
    ontology_key: str = "output.syno",
    max_tries: int = 3,
) -> Tuple[str, bool]:
    """Valida uma entrada ONTOLOGY via synesis.load(). Se inválida, solicita correção.

    Valida o .syno gerado carregando-o junto com as anotações existentes
    do projeto para que o compilador possa resolver referências a códigos.

    Args:
        output: Texto Synesis com o bloco ONTOLOGY gerado pelo LLM.
        ctx: Contexto do projeto retornado por load_project().
        llm_client: Cliente LLM para solicitar correções.
        ontology_key: Nome virtual do arquivo .syno para synesis.load().
        max_tries: Número máximo de tentativas de correção (padrão: 3).

    Returns:
        (output_final, success) — success=False se todas as tentativas falharam.
    """
    last_errors = ""
    output = _strip_markdown_fences(output)
    extracted = _extract_ontology_blocks(output)
    if extracted:
        output = extracted

    for attempt in range(max_tries + 1):
        try:
            result = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents=ctx.get("annotation_contents") or None,
                ontology_contents={ontology_key: output},
                bibliography_content=ctx.get("bib_content"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                llm_client.fix(output, last_errors, temperature=temperature)
            )
            extracted = _extract_ontology_blocks(raw)
            output = extracted or raw
            continue

        if not _has_structural_errors(result):
            return output, True

        last_errors = result.get_diagnostics()

        if attempt >= max_tries:
            break

        temperature = CORRECTION_TEMPERATURES[
            min(attempt, len(CORRECTION_TEMPERATURES) - 1)
        ]
        raw = _strip_markdown_fences(
            llm_client.fix(output, last_errors, temperature=temperature)
        )
        extracted = _extract_ontology_blocks(raw)
        output = extracted or raw

    error_header = (
        f"# ERRO: validação falhou após {max_tries} tentativa(s)\n"
        f"# Último diagnóstico:\n"
    )
    commented_errors = "\n".join(f"# {line}" for line in last_errors.splitlines())
    return error_header + commented_errors + "\n\n" + output, False


async def validate_ontology_entry_async(
    output: str,
    ctx: dict,
    llm_client: "LLMClient",
    ontology_key: str = "output.syno",
    max_tries: int = 3,
) -> Tuple[str, bool]:
    """Versão assíncrona de validate_ontology_entry()."""
    last_errors = ""
    output = _strip_markdown_fences(output)
    extracted = _extract_ontology_blocks(output)
    if extracted:
        output = extracted

    for attempt in range(max_tries + 1):
        try:
            result = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents=ctx.get("annotation_contents") or None,
                ontology_contents={ontology_key: output},
                bibliography_content=ctx.get("bib_content"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                await llm_client.fix_async(output, last_errors, temperature=temperature)
            )
            extracted = _extract_ontology_blocks(raw)
            output = extracted or raw
            continue

        if not _has_structural_errors(result):
            return output, True

        last_errors = result.get_diagnostics()

        if attempt >= max_tries:
            break

        temperature = CORRECTION_TEMPERATURES[
            min(attempt, len(CORRECTION_TEMPERATURES) - 1)
        ]
        raw = _strip_markdown_fences(
            await llm_client.fix_async(output, last_errors, temperature=temperature)
        )
        extracted = _extract_ontology_blocks(raw)
        output = extracted or raw

    error_header = (
        f"# ERRO: validação falhou após {max_tries} tentativa(s)\n"
        f"# Último diagnóstico:\n"
    )
    commented_errors = "\n".join(f"# {line}" for line in last_errors.splitlines())
    return error_header + commented_errors + "\n\n" + output, False


def _extract_ontology_blocks(text: str) -> str:
    """Extrai apenas os blocos ONTOLOGY...END ONTOLOGY do output do LLM."""
    import re

    pattern = re.compile(
        r"^ONTOLOGY\s+\S+.*?^END ONTOLOGY",
        re.MULTILINE | re.DOTALL,
    )
    blocks = pattern.findall(text)
    if not blocks:
        return ""
    return "\n\n".join(block.strip() for block in blocks)


def _strip_markdown_fences(text: str) -> str:
    """Remove delimitadores de bloco de código markdown do output do LLM.

    LLMs frequentemente envolvem o output em ```...``` mesmo quando instruídos
    a não fazê-lo. Esta função remove esses delimitadores para que o compilador
    Synesis receba texto limpo.
    """
    import re

    # Remove ``` ou ```synesis ou ```syn (com ou sem newline após)
    stripped = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    stripped = re.sub(r"\n?```$", "", stripped.strip())
    return stripped.strip()
