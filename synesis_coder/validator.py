"""Validação e correção de output Synesis via compilador.

O ciclo de correção usa temperature escalation para evitar loop
determinístico quando temperature=0:
    Tentativa 0: não aplicável (output já gerado)
    Tentativa 1 de correção: temperature=0.0
    Tentativa 2 de correção: temperature=0.2
    Tentativa 3 de correção: temperature=0.5
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Tuple

import synesis

if TYPE_CHECKING:
    from synesis_coder.debug_log import DebugRecorder
    from synesis_coder.llm_client import LLMClient

_log = logging.getLogger(__name__)

# Temperature escalation: cada elemento corresponde a uma tentativa de correção
CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]

_FIX_REJECTED_MSG = (
    "Correção rejeitada: o LLM devolveu MENOS blocos do que recebeu "
    "(truncagem). Mantendo a versão anterior e seguindo para a próxima "
    "tentativa."
)


def _fix_system_prompt(ctx: dict, scope: str) -> str | None:
    """Reconstrói o system prompt da geração para reenviar na correção.

    Sem isso, a chamada de correção vai à API sem as GUIDELINES do template
    (réguas de score, proibições de domínio, code_index) — o modelo corrige
    enxergando apenas o diagnóstico estrutural do compilador, e o resultado
    degrada a cada iteração do laço.

    O texto é reconstruído a partir do mesmo `ctx` usado na geração, e
    `_build_*_system_prompt` é determinístico (sem timestamp/uuid/set), de modo
    que o prefixo casa byte-a-byte com o cache já gravado — o reenvio custa
    ~0.1x em vez de 1.0x.

    Args:
        ctx: Contexto do projeto (o mesmo passado à geração).
        scope: "item" para blocos ITEM isolados (modos item/document/refine);
            "abstract" para SOURCE + ITEMs (modos abstract/dataset);
            "ontology" para entradas ONTOLOGY (modo ontology).

    Returns:
        O system prompt, ou None se não for possível reconstruí-lo (nesse caso
        a correção mantém o comportamento antigo, sem contexto).
    """
    try:
        from synesis_coder.prompt_builder import (
            _build_abstract_system_prompt,
            _build_ontology_system_prompt,
            _build_system_prompt,
        )

        if scope == "abstract":
            return _build_abstract_system_prompt(ctx)
        if scope == "ontology":
            return _build_ontology_system_prompt(ctx)
        return _build_system_prompt(ctx)
    except Exception:  # pragma: no cover - degradação graciosa
        # Falha ao remontar não pode derrubar a validação: sem system o fix
        # volta ao comportamento anterior (cego), que ainda funciona.
        return None


def _count_blocks(text: str) -> tuple[int, int, int]:
    """Conta blocos ITEM, SOURCE e ONTOLOGY. Determinístico, sem IO."""
    import re

    return (
        len(re.findall(r"^\s*ITEM\b", text, re.MULTILINE)),
        len(re.findall(r"^\s*SOURCE\b", text, re.MULTILINE)),
        len(re.findall(r"^\s*ONTOLOGY\b", text, re.MULTILINE)),
    )


def _accept_fix(previous: str, candidate: str) -> tuple[str, bool]:
    """Rejeita uma correção que PERDE blocos em vez de consertá-los.

    O loop de correção já produziu truncagem em produção: um caso documentado
    passou de 19 ITEMs para 1, com perda do bloco SOURCE. Como a saída ainda é
    sintaticamente válida, nada no pipeline detectava a perda — o `.syn` era
    gravado silenciosamente mutilado.

    A guarda é deliberadamente conservadora: só rejeita PERDA (menos blocos de
    um tipo que já existia). Correções que mantêm ou aumentam a contagem
    passam, porque dividir um bloco malformado em dois é resultado legítimo.

    Returns:
        (texto_a_usar, aceito) — quando rejeitado, devolve `previous` intacto.
    """
    if not candidate.strip():
        return previous, False

    for prev_n, cand_n in zip(_count_blocks(previous), _count_blocks(candidate)):
        if prev_n and cand_n < prev_n:
            return previous, False
    return candidate, True


# NOTA — por que o validator NÃO passa `schema=` ao fix
#
# `fix()`/`fix_async()` aceitam `schema=`, mas este módulo deliberadamente não
# o usa. O motivo é um descasamento de formato, não um esquecimento:
#
#   - Com schema, o modelo devolve JSON de VALORES ({"items": [...]}), que só
#     vira bloco Synesis depois de passar pelo `block_assembler`.
#   - O laço abaixo trata o retorno do fix como TEXTO Synesis: aplica
#     `_extract_item_blocks`/`_extract_annotation_blocks` e entrega direto a
#     `synesis.load()`.
#
# Passar o schema aqui faria `_extract_*` devolver string vazia e o JSON cru
# seguir para o compilador, que falharia em toda tentativa — trocando uma
# correção degradada por uma correção impossível.
#
# Fechar o defeito irmão (fix perde as garantias estruturais do schema) exige
# montar um caminho JSON completo para a correção: prompt de valores + envio do
# schema + `block_assembler` no retorno. É mudança de escopo maior, fora desta
# correção. Ver Planning/Estudo_Fix_Perde_System_Prompt.md §6.2.


def validate_and_fix(
    output: str,
    ctx: dict,
    llm_client: "LLMClient",
    annotation_key: str = "output.syn",
    max_tries: int = 3,
    scope: str = "item",
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
        scope: "item" (blocos ITEM) ou "abstract" (SOURCE + ITEMs) — determina
            qual system prompt é reenviado nas correções.

    Returns:
        (output_final, success) — success=False se todas as tentativas falharam.
    """
    last_errors = ""
    fix_system = _fix_system_prompt(ctx, scope)
    output = _strip_markdown_fences(output)
    output = _extract_item_blocks(output)

    for attempt in range(max_tries + 1):
        try:
            result = synesis.load(
                project_content=ctx["project_content"],
                template_content=ctx["template_content"],
                annotation_contents={annotation_key: output},
                bibliography_content=ctx.get("bib_content"),
                dataset_index=ctx.get("dataset_index"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                llm_client.fix(
                    output, last_errors, temperature=temperature, system=fix_system,
                )
            )
            candidate = _extract_item_blocks(raw) or raw
            output, accepted = _accept_fix(output, candidate)
            if not accepted:
                _log.warning(_FIX_REJECTED_MSG)
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
            llm_client.fix(
                output, last_errors, temperature=temperature, system=fix_system,
            )
        )
        candidate = _extract_item_blocks(raw) or raw
        output, accepted = _accept_fix(output, candidate)
        if not accepted:
            _log.warning(_FIX_REJECTED_MSG)

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
    scope: str = "abstract",
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
        scope: "abstract" (SOURCE + ITEMs — padrão dos modos de lote) ou "item"
            (apenas blocos ITEM, ex.: document/refine). Determina qual system
            prompt é reenviado nas correções.

    Returns:
        (output_final, success) — success=False se todas as tentativas falharam.
    """
    last_errors = ""
    fix_system = _fix_system_prompt(ctx, scope)
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
                dataset_index=ctx.get("dataset_index"),
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
                    output, last_errors, temperature=temperature, context=context,
                    system=fix_system,
                )
            )
            candidate = _extract_annotation_blocks(raw) or raw
            output, accepted = _accept_fix(output, candidate)
            if not accepted:
                _log.warning(_FIX_REJECTED_MSG)
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
                output, last_errors, temperature=temperature, context=context,
                system=fix_system,
            )
        )
        candidate = _extract_annotation_blocks(raw) or raw
        output, accepted = _accept_fix(output, candidate)
        if not accepted:
            _log.warning(_FIX_REJECTED_MSG)

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
    fix_system = _fix_system_prompt(ctx, "ontology")
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
                dataset_index=ctx.get("dataset_index"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                llm_client.fix(
                    output, last_errors, temperature=temperature, system=fix_system,
                )
            )
            candidate = _extract_ontology_blocks(raw) or raw
            output, accepted = _accept_fix(output, candidate)
            if not accepted:
                _log.warning(_FIX_REJECTED_MSG)
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
            llm_client.fix(
                output, last_errors, temperature=temperature, system=fix_system,
            )
        )
        candidate = _extract_ontology_blocks(raw) or raw
        output, accepted = _accept_fix(output, candidate)
        if not accepted:
            _log.warning(_FIX_REJECTED_MSG)

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
    fix_system = _fix_system_prompt(ctx, "ontology")
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
                dataset_index=ctx.get("dataset_index"),
            )
        except Exception as exc:
            last_errors = f"Erro de parse: {exc}"
            if attempt >= max_tries:
                break
            temperature = CORRECTION_TEMPERATURES[
                min(attempt, len(CORRECTION_TEMPERATURES) - 1)
            ]
            raw = _strip_markdown_fences(
                await llm_client.fix_async(
                    output, last_errors, temperature=temperature, system=fix_system,
                )
            )
            candidate = _extract_ontology_blocks(raw) or raw
            output, accepted = _accept_fix(output, candidate)
            if not accepted:
                _log.warning(_FIX_REJECTED_MSG)
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
            await llm_client.fix_async(
                output, last_errors, temperature=temperature, system=fix_system,
            )
        )
        candidate = _extract_ontology_blocks(raw) or raw
        output, accepted = _accept_fix(output, candidate)
        if not accepted:
            _log.warning(_FIX_REJECTED_MSG)

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
    """Limpa o output do LLM: remove fences markdown e canoniza a indentação.

    LLMs frequentemente envolvem o output em ```...``` mesmo quando instruídos
    a não fazê-lo. Esta função remove esses delimitadores para que o compilador
    Synesis receba texto limpo.

    Também normaliza a indentação dos blocos (`normalize_indentation`): a forma
    do bloco é responsabilidade do Python, não do modelo. No caminho JSON o
    `block_assembler` já garante isso por construção; aqui a mesma garantia
    alcança o caminho de texto livre, onde o LLM digita o bloco inteiro. Sem
    isso, um modelo que esquece de indentar perde o registro por erro de parse
    — medido em produção com `inclusionai/ling-2.6-flash`.

    É o ponto de passagem obrigatório de todo texto que chega ao validador
    (12 sítios), o que torna a normalização universal sem tocar em cada laço.
    """
    import re

    from synesis_coder.block_assembler import normalize_indentation

    # Remove ``` ou ```synesis ou ```syn (com ou sem newline após)
    stripped = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    stripped = re.sub(r"\n?```$", "", stripped.strip())
    return normalize_indentation(stripped.strip())
