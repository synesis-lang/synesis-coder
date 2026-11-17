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
    - campaign_summary(...): sumário final unificado de uma campanha em lote.

Dependencies:
    - importlib.metadata: versões instaladas de synesis-coder e synesis.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import logging
from importlib.metadata import version as _pkg_version
from typing import Optional

logger = logging.getLogger(__name__)


def short_model_name(model: str) -> str:
    """Nome de modelo legível a partir do ID cru.

    IDs de modelos locais carregam repositório, arquivo e quantização
    (`hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:Qwen3.8-27B-...-IQ3_S`), o que
    ocupa a linha inteira sem informar mais que o nome. Corta o caminho e
    mantém a última parte, que é a que identifica o modelo.
    """
    if not isinstance(model, str):
        return str(model)
    name = model.rsplit(":", 1)[-1] if ":" in model else model
    return name.rsplit("/", 1)[-1]


def _counter(usage, name: str) -> int:
    """Lê um contador de `usage`, com 0 quando ausente ou não-numérico.

    Defensivo pelo mesmo motivo de `llm_client._int_attr`: nos testes o cliente
    costuma ser um MagicMock, cujo getattr devolve outro MagicMock — que não
    formata nem compara como número. Sem a checagem de tipo, o sumário derruba
    a execução em vez de degradar.
    """
    value = getattr(usage, name, 0)
    return value if isinstance(value, int) else 0


def _thousands(n: int) -> str:
    """Inteiro com vírgula como separador de milhar (saída do CLI em inglês)."""
    return f"{n:,}"


def format_duration(seconds: float) -> str:
    """Duração legível: `42s`, `6m45s`, `2h13m`.

    Segundos crus deixam o leitor calculando: `1005.0s` não diz "16 minutos".
    """
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def campaign_summary(
    *,
    mode: str,
    project: str,
    total: int,
    ok: int,
    failed: int,
    elapsed: float,
    output: str,
    llm_client=None,
    unit: str = "records",
    skipped: int = 0,
    extra: Optional[list[tuple[str, str]]] = None,
) -> str:
    """Monta o sumário final de uma campanha em lote.

    Reúne num só lugar o que antes cada modo formatava à sua maneira — e o que
    vários mediam sem exibir. Três blocos, na ordem em que o pesquisador
    pergunta: o que rodou, o que custou, quanto demorou.

    A linha `Tentativas` é a que faltava: `chamadas` acima de `total` revela
    correções e fallbacks, que encarecem a campanha sem aparecer no contador de
    OK/falhas. `Ritmo` decide entre backend local e API paga.

    Args:
        mode: Nome do modo (`abstract`, `dataset`, ...).
        project: Identificador do projeto (stem do .synp).
        total: Unidades processadas nesta execução.
        ok: Unidades concluídas com sucesso.
        failed: Unidades que falharam.
        elapsed: Duração total em segundos.
        output: Destino da saída (caminho ou descrição).
        llm_client: Cliente LLM; quando informado, acrescenta modelo, tentativas
            e tokens. Sem ele o sumário sai só com contagem e tempo.
        unit: Rótulo da unidade ("referências", "registros", "chunks"...).
        skipped: Unidades puladas por `--resume`.
        extra: Linhas adicionais do modo, como `[("Concordância", "0.722")]`.

    Returns:
        Bloco de texto pronto para `logger.info`.
    """
    sep = "─" * 52
    rate = (ok / total * 100) if total else 0.0
    lines = [
        "",
        sep,
        f"  Campaign    {mode} · {project}",
    ]

    if llm_client is not None:
        lines.append(f"  Model       {short_model_name(llm_client.model)}")

    lines.append("")
    lines.append(
        f"  {unit.capitalize():11} {total} total · {ok} OK ({rate:.0f}%) · "
        f"{failed} failures"
    )
    if skipped:
        lines.append(f"  Resumed     {skipped} already processed (skipped)")

    if llm_client is not None:
        usage = llm_client.usage
        calls = _counter(usage, "api_calls")
        corrections = _counter(usage, "corrections")
        fallbacks = _counter(usage, "schema_fallbacks")
        tok_in = _counter(usage, "total_prompt_tokens")
        tok_out = _counter(usage, "output_tokens")
        tok_cache = _counter(usage, "cache_read_tokens")

        lines.append("")
        attempts = [f"calls {calls}"]
        if corrections:
            attempts.append(f"corrections {corrections}")
        if fallbacks:
            attempts.append(f"fallbacks {fallbacks}")
        lines.append(f"  Attempts    {' · '.join(attempts)}")

        tokens = [f"in {_thousands(tok_in)}", f"out {_thousands(tok_out)}"]
        if tok_cache:
            tokens.append(f"cache r {_thousands(tok_cache)}")
        lines.append(f"  Tokens      {' · '.join(tokens)}")

        pace = []
        if total and elapsed > 0:
            pace.append(f"{elapsed / total:.1f}s per {unit[:3]}")
        if elapsed > 0 and tok_out:
            # Vazão agregada do lote, não velocidade do modelo: com
            # concorrência > 1 há gerações simultâneas, e o valor fica acima do
            # que uma única chamada atinge. Serve para comparar campanhas
            # inteiras (local vs. API), não para aferir o modelo.
            pace.append(f"{tok_out / elapsed:.0f} tok/s in batch")
        if pace:
            lines.append(f"  Pace        {' · '.join(pace)}")

    for label, value in extra or []:
        lines.append(f"  {label:11} {value}")

    lines.append("")
    lines.append(f"  Time        {format_duration(elapsed)}")
    lines.append(f"  Output      {output}")
    lines.append(sep)
    return "\n".join(lines)


def _safe_version(package: str) -> str:
    """Versão instalada de `package`, ou '?' quando indisponível."""
    try:
        return _pkg_version(package)
    except Exception:
        return "?"


def build_engine_banner(
    facts, *, concurrent: Optional[int] = None, json_path: bool = True,
    thinking_off: bool | str = False,
) -> str:
    """Bloco de abertura descrevendo em que condições a campanha vai rodar.

    Só informa o que o provedor de fato soube dizer: parâmetros e quantização
    existem apenas em modelos locais, e a janela de contexto em 4 dos 5
    provedores. Eixo ausente é OMITIDO — "desconhecido" ocuparia a linha sem
    informar nada.

    Args:
        facts: ModelFacts do motor.
        concurrent: Chamadas simultâneas configuradas.
        json_path: True quando a extração roda pelo caminho JSON (schema).
        thinking_off: True quando o raciocínio interno foi desativado.

    Returns:
        Bloco de 1-3 linhas, sem separadores (é abertura, não sumário).
    """
    lines = []

    modelo = [facts.display_name]
    if facts.parameters:
        modelo.append(f"{facts.parameters} parameters")
    # IDs de GGUF costumam já terminar na quantização ("...-IQ3_S"): repeti-la
    # como segmento próprio seria redundante.
    if facts.quantization and facts.quantization not in facts.display_name:
        modelo.append(facts.quantization)
    if not facts.parameters and facts.context_window:
        modelo.append(f"window {_thousands(facts.context_window)}")
    lines.append(f"  Model       {' · '.join(modelo)}")

    if facts.host:
        servidor = [f"{facts.source or 'server'} ({facts.host})"]
        if facts.context_served and facts.context_window:
            servidor.append(
                f"context {_thousands(facts.context_served)} "
                f"of {_thousands(facts.context_window)}"
            )
        elif facts.context_window:
            servidor.append(f"model window {_thousands(facts.context_window)}")
        lines.append(f"  Server      {' · '.join(servidor)}")

    execucao = []
    if concurrent is not None:
        execucao.append(f"concurrency {concurrent}")
    execucao.append("JSON extraction" if json_path else "free-text extraction")
    if thinking_off:
        execucao.append(thinking_off if isinstance(thinking_off, str) else "reasoning off")
    lines.append(f"  Run         {' · '.join(execucao)}")

    return "\n".join(lines)


def engine_warnings(facts, *, concurrent: Optional[int] = None) -> list[str]:
    """Avisos de configuração que se pode corrigir ANTES de a campanha rodar.

    Cada aviso corresponde a um incidente medido: um servidor entregando 1,5%
    da janela do modelo, e o default de 5 chamadas simultâneas contra uma GPU
    que as serializa.

    Returns:
        Lista de mensagens; vazia quando nada há a ajustar.
    """
    warnings = []

    if facts.context_at_vendor_default:
        warnings.append(
            f"The server is serving {_thousands(facts.context_served)} tokens "
            f"of context — the factory default, probably not configured. "
            f"This coder's prompts exceed 7,000 tokens of template alone, "
            f"so calls tend to fail. Set OLLAMA_CONTEXT_LENGTH "
            f"on the server to the largest value the VRAM allows (the KV cache "
            f"grows with the context and competes for memory with the model weights)."
        )

    if facts.is_local and concurrent is not None and concurrent > 1:
        warnings.append(
            f"Concurrency {concurrent} against a local server: the calls "
            f"compete for the same GPU instead of running in parallel, and the context "
            f"may be split among them. Use --concurrent 1."
        )

    return warnings


def warn_prompt_too_large(facts, estimated_tokens: int, *, margin: float = 0.9) -> bool:
    """Alerta quando o prompt montado não cabe na janela servida.

    O prompt é montado localmente, então seu tamanho é conhecido ANTES do
    envio: comparar com a janela é aritmética, não adivinhação. Sem isto o
    excesso só aparece como erro 400 — depois de a chamada ter sido paga.

    Args:
        facts: ModelFacts do motor.
        estimated_tokens: Tamanho estimado do prompt.
        margin: Fração da janela acima da qual já se alerta (a resposta também
            ocupa espaço).

    Returns:
        True se o aviso foi emitido.
    """
    limit = facts.context_served or facts.context_window
    if not limit or estimated_tokens < limit * margin:
        return False

    logger.warning(
        "The estimated prompt (%s tokens) takes almost the whole available window "
        "(%s). The response also needs room — the call tends to "
        "truncate. Reduce what is sent or enlarge the server window.",
        _thousands(estimated_tokens), _thousands(limit),
    )
    return True


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
        line += " (upgrade 'anthropic>=0.77.1' for the JSON path)"

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
        "%d record(s) generated as FREE TEXT — the JSON path was "
        "abandoned and the schema guarantees (enum, minimum/maximum, "
        "additionalProperties) did NOT apply to them. Check these "
        "records by hand; raising SYNESIS_CODER_MAX_TOKENS usually "
        "removes the cause.",
        n,
    )


def runtime_banner(
    llm_client, format: str = "plain", concurrent: Optional[int] = None,
) -> None:
    """Emite o banner de abertura via logger.info (stderr na CLI).

    Usar logger — não print/stdout — preserva o stdout do formato `plain`, que
    carrega o `.syn` cru destinado a arquivo/editor. O nível é centralizado pela
    CLI (`_configure_logging`), então `-q`/`-qq` silenciam o banner naturalmente.

    Busca os metadados do motor (janela de contexto, parâmetros, quantização)
    e emite os avisos de configuração corrigíveis. A busca degrada em silêncio:
    provedor que não informa, ou rede indisponível, resulta em banner mais
    curto — nunca em falha.

    Args:
        llm_client: Cliente LLM em uso.
        format: Reservado; o canal (stderr) é o que protege o stdout.
        concurrent: Chamadas simultâneas configuradas, quando o modo as tem.
    """
    from synesis_coder import model_facts as _mf

    try:
        facts = _mf.fetch_model_facts(llm_client)
    except Exception as exc:
        # Cinto e suspensório: fetch_model_facts já degrada por dentro, mas o
        # banner não pode depender disso. Relatar a execução jamais pode
        # impedi-la.
        logger.debug("Engine metadata unavailable: %s", exc)
        facts = _mf.ModelFacts(
            display_name=_mf.short_name(getattr(llm_client, "model", "") or "")
        )

    json_path = llm_client.supports_json_schema()
    thinking_off = _thinking_disabled(llm_client)

    logger.info(
        "\n%s",
        build_engine_banner(
            facts,
            concurrent=concurrent,
            json_path=json_path,
            thinking_off=thinking_off,
        ),
    )

    if not json_path and llm_client.backend == "anthropic":
        logger.info(
            "Upgrade 'anthropic>=0.77.1' to enable the JSON path."
        )

    for warning in engine_warnings(facts, concurrent=concurrent):
        logger.warning(warning)


def _thinking_disabled(llm_client) -> str:
    """Rótulo do controle de raciocínio que o cliente de fato envia ("" se nenhum).

    Lê do próprio `llm_client` o que vai no payload, em vez de repetir a lista:
    duplicá-la faria o banner divergir do que é de fato enviado — anunciando
    "raciocínio desativado" onde o modelo continua pensando (foi o caso do
    qwen3.8 no OpenRouter antes de `_reasoning_off_params`), ou omitindo onde
    está desativado.
    """
    from synesis_coder.llm_client import _reasoning_off_params, _thinking_off_params

    model = getattr(llm_client, "model", "") or ""
    if not isinstance(model, str):
        return ""
    backend = getattr(llm_client, "backend", "openai")
    if backend == "anthropic":
        # Nos Claude 5.x o raciocínio é LIGADO por padrão e nem sempre desliga
        # (Opus 5.5): o rótulo vem do mesmo plano que monta a requisição.
        label = getattr(llm_client, "thinking_label", None)
        try:
            text = label(False) if callable(label) else ""
        except Exception:  # noqa: BLE001 — banner é informação, não condição
            text = ""
        return text if isinstance(text, str) else ""
    if backend != "openai":
        return "reasoning off" if _thinking_off_params(model) else ""
    params = _reasoning_off_params(model, getattr(llm_client, "_api_url", "") or "")
    effort = (params.get("reasoning") or {}).get("effort") or params.get("reasoning_effort")
    if effort and effort != "none":
        return f"minimal reasoning ({effort})"
    return "reasoning off" if params else ""


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
