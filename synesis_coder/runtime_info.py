"""runtime_info.py - Banner de status de execução para o usuário pesquisador.

Purpose:
    Emite uma linha única, legível por não-técnicos, informando em que condições
    o coder está rodando: versão do synesis-coder, versão do compilador synesis,
    backend + modelo LLM em uso e — crucialmente — se o caminho ativo é o
    "JSON assembler" (determinístico) ou "texto-livre" (extração por regex).

    O caminho determinístico (Opção 3) só ativa quando o backend suporta
    response_format json_schema; no backend padrão (anthropic) o coder cai no
    caminho de texto livre, sem nenhum sinal visível até agora.

Components:
    - runtime_banner(llm_client, format): monta e emite a linha via logger.

Dependencies:
    - importlib.metadata: versões instaladas de synesis-coder e synesis.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import logging
from importlib.metadata import version as _pkg_version

logger = logging.getLogger(__name__)


def _safe_version(package: str) -> str:
    """Versão instalada de `package`, ou '?' quando indisponível."""
    try:
        return _pkg_version(package)
    except Exception:
        return "?"


def build_banner_line(llm_client, format: str = "plain") -> str:
    """Monta a linha de status (sem emitir).

    Args:
        llm_client: LLMClient já instanciado (expõe backend, model,
            supports_json_schema()).
        format: Reservado para diferenciação futura entre plain/verbose; a linha
            é idêntica nos dois — o canal (stderr via logger) é que protege o
            stdout do modo plain.

    Returns:
        Linha única de status.
    """
    json_path = llm_client.supports_json_schema()
    path_label = "JSON assembler" if json_path else "texto-livre (regex)"

    line = f"{llm_client.backend}/{llm_client.model} | {path_label}"

    if not json_path and llm_client.backend == "anthropic":
        # Anthropic em texto-livre só ocorre com SDK < 0.77 (sem structured
        # outputs). A correção é atualizar o SDK, não trocar de backend.
        line += " (atualize 'anthropic>=0.77.1' para o caminho JSON)"

    return line


def warn_schema_fallbacks(llm_client) -> None:
    """Alerta que algum registro perdeu as garantias do schema, se tiver ocorrido.

    O caminho JSON pode ser abandonado em runtime — orçamento de tokens esgotado
    no raciocínio, resposta não-JSON, ou recusa do backend. Quando isso acontece
    o registro é gerado em TEXTO LIVRE: sem `enum` (ENUMERATED/ORDERED), sem
    `minimum`/`maximum` (SCALE), sem `additionalProperties: false`. O bloco sai
    sintaticamente válido e é contabilizado como OK.

    Sem este aviso o efeito é indetectável no formato padrão: o contador existe
    em `usage.summary_line()`, mas essa linha só é emitida com `--format
    verbose`. O pesquisador veria "OK: 3 (100%)" sem saber que parte do corpus
    rodou sem as restrições derivadas do template.

    Emitido em WARNING (não INFO) porque é uma condição corrigível — aumentar
    SYNESIS_CODER_MAX_TOKENS resolve o caso dominante — e porque o silêncio aqui
    compromete a validade do dado, não apenas o custo.
    """
    n = getattr(llm_client.usage, "schema_fallbacks", 0)
    if not n:
        return
    logger.warning(
        "%d registro(s) gerado(s) em TEXTO LIVRE — o caminho JSON foi "
        "abandonado e as garantias do schema (enum, minimum/maximum, "
        "additionalProperties) NÃO se aplicaram a eles. Verifique esses "
        "registros manualmente; aumentar SYNESIS_CODER_MAX_TOKENS costuma "
        "eliminar a causa.",
        n,
    )


def runtime_banner(llm_client, format: str = "plain") -> None:
    """Emite o banner de status via logger.info (stderr na CLI).

    Usar logger — não print/stdout — preserva o stdout do formato `plain`, que
    carrega o `.syn` cru destinado a arquivo/editor. O nível é centralizado pela
    CLI (`_configure_logging`), então `-q`/`-qq` silenciam o banner naturalmente.
    """
    logger.info("Motor: %s", build_banner_line(llm_client, format))


def print_product_header(quiet: int = 0) -> None:
    """Imprime o cabeçalho do produto em stderr (uma vez por invocação).

    Suprimido com -qq (quiet >= 2). Escrito diretamente em stderr — antes
    que o logging esteja configurado — para aparecer sempre antes de qualquer
    linha [INFO]/[WARN].
    """
    import sys

    if quiet >= 2:
        return

    coder_v = _safe_version("synesis-coder")
    core_v = _safe_version("synesis")
    sys.stderr.write(
        f"SYNESIS CODER (v{coder_v}) | Core (v{core_v})\n"
        "Extraction engine for generating valid annotations in the Synesis ecosystem.\n"
        "The template defines all fields, relations, and constraints — nothing is hardcoded.\n"
        "\n"
    )
