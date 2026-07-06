"""debug_log.py - Auditoria legível do pipeline LLM (flag --debug).

Purpose:
    Acumula eventos do pipeline de codificação (chamadas LLM, ciclos de
    validação e correção) e os renderiza como um relatório Markdown legível
    por pesquisadores não-técnicos. Ativado pela flag --debug da CLI.

Components:
    - DebugRecorder: acumulador thread-safe de eventos, com contexto por chunk
      propagado via threading.local (chamadas LLM rodam em threads worker).
    - classify_error: categoriza um erro do compilador como "structural" ou
      "value" (mesma taxonomia da instrumentação da Opção 0).
    - translate_diagnostics: converte erros do compilador em frases amigáveis.
    - render_markdown / write: produzem e persistem o relatório final.

Dependencies:
    - synesis_coder.token_usage: leitura de contadores de tokens.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Literal, Optional, Tuple

# ---------------------------------------------------------------------------
# Classificação e tradução de erros do compilador
# ---------------------------------------------------------------------------

# Nomes de classe de erro (synesis.ast.results) que são corrigíveis por
# estrutura fixa (esqueleto/JSON) — o LLM errou a forma, não o conteúdo.
_STRUCTURAL_ERROR_NAMES = frozenset(
    {
        "MissingRequiredField",      # E020
        "ForbiddenFieldPresent",     # E021
        "UnknownFieldName",          # E022
        "MissingBundleField",        # E016
        "BundleCountMismatch",       # E017
        "DuplicateFieldName",        # E069
        "EmptyItemBlock",            # E067
        "MalformedQualifiedChain",   # E011
        "ChainWithoutArrowOperator", # parse-like
    }
)

# Erros que dependem do conteúdo escolhido pelo LLM — estrutura não resolve.
_VALUE_ERROR_NAMES = frozenset(
    {
        "InvalidChainRelation",      # E010
        "ScaleOutOfRange",           # E030
        "InvalidEnumeratedValue",    # E027
        "InvalidOrderedValue",       # E029
        "ChainArityViolation",       # E007
        "UndefinedCode",             # E004
        "DecimalInIntegerScale",     # E044
        "DuplicateCodeInField",      # E045
    }
)


def classify_error(err: Any) -> Literal["structural", "value", "other"]:
    """Categoriza um erro do compilador pela classe.

    "structural" → eliminável por construção (esqueleto/JSON Schema).
    "value"      → depende do conteúdo gerado pelo LLM.
    "other"      → fora das duas categorias conhecidas.
    """
    name = type(err).__name__
    if name in _STRUCTURAL_ERROR_NAMES:
        return "structural"
    if name in _VALUE_ERROR_NAMES:
        return "value"
    return "other"


def _friendly_line(err: Any) -> str:
    """Frase amigável de uma linha para um erro do compilador.

    Reaproveita to_cli_line() do compilador (já em português, voltado ao
    usuário final) e anexa o código técnico como nota secundária.
    """
    code = getattr(err, "CODE", "")
    try:
        text = err.to_cli_line()
    except Exception:
        text = str(err)
    suffix = f" *(código técnico: {code})*" if code else ""
    return f"{text}{suffix}"


def translate_diagnostics(result: Any) -> List[Tuple[str, str]]:
    """Extrai (categoria, frase amigável) de cada erro estrutural do resultado.

    Ignora OrphanItem (esperado ao validar ITEM isolado). Quando o resultado
    não expõe a lista de erros (ex.: erro de parse), retorna lista vazia — o
    chamador usa o diagnóstico textual bruto nesse caso.
    """
    try:
        errors = result.validation_result.errors
    except AttributeError:
        return []

    out: List[Tuple[str, str]] = []
    for err in errors:
        if type(err).__name__ == "OrphanItem":
            continue
        out.append((classify_error(err), _friendly_line(err)))
    return out


# ---------------------------------------------------------------------------
# Eventos acumulados
# ---------------------------------------------------------------------------


@dataclass
class LLMCallEvent:
    """Uma chamada bruta ao LLM (geração ou correção)."""

    phase: str                  # "source" | "chunk" | "fix"
    context: Optional[tuple]    # ("chunk", index, total) ou None
    system: str
    user: str
    raw: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    model: str
    temperature: Optional[float]
    max_tokens: int


@dataclass
class ValidationEvent:
    """Resultado de uma tentativa de validação de um chunk."""

    context: Optional[tuple]
    attempt: int
    submitted: str
    success: bool
    diagnostics: List[Tuple[str, str]]   # (categoria, frase amigável)
    raw_diagnostic: str                  # fallback textual (erros de parse)


@dataclass
class ChunkSummary:
    """Resumo do resultado de um chunk."""

    context: tuple
    items_generated: int
    corrections: int
    success: bool


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class DebugRecorder:
    """Acumula eventos do pipeline e renderiza um relatório Markdown.

    Thread-safe: chunks são processados concorrentemente e as chamadas LLM
    rodam em threads worker (asyncio.to_thread). O contexto do chunk é
    propagado por threading.local — o chamador seta set_context() antes de
    cada chamada e o evento bruto, emitido na thread worker, o lê.

    Quando o recorder não é injetado (sem --debug), nenhum overhead ocorre:
    os pontos de instrumentação são guardados por `if recorder is not None`.
    """

    def __init__(
        self,
        unit_type: str = "chunk",
        unit_label: str = "Trecho",
        coding_step_title: str = "Etapa 2 — Codificação dos trechos",
    ) -> None:
        """Inicializa o recorder.

        Args:
            unit_type: primeiro elemento do contexto que delimita uma unidade
                de processamento ("chunk" no modo document, "entry" no abstract).
            unit_label: rótulo legível da unidade no relatório ("Trecho",
                "Referência").
            coding_step_title: título da seção que lista as unidades.
        """
        self.unit_type = unit_type
        self.unit_label = unit_label
        self.coding_step_title = coding_step_title
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._llm_calls: List[LLMCallEvent] = []
        self._validations: List[ValidationEvent] = []
        self._chunk_summaries: List[ChunkSummary] = []
        self._header: dict = {}
        self._footer: dict = {}

    # -- contexto por thread -------------------------------------------------

    def set_context(self, context: Optional[tuple]) -> None:
        """Define o contexto (ex.: ("chunk", 2, 7)) para a thread atual."""
        self._tls.context = context

    def _get_context(self) -> Optional[tuple]:
        return getattr(self._tls, "context", None)

    # -- gravação de eventos -------------------------------------------------

    def record_session_header(self, **info: Any) -> None:
        with self._lock:
            self._header = dict(info)

    def record_session_footer(self, **info: Any) -> None:
        with self._lock:
            self._footer = dict(info)

    def record_llm_call(
        self,
        *,
        phase: str,
        system: str,
        user: str,
        raw: str,
        latency_ms: float,
        input_tokens: int,
        output_tokens: int,
        model: str,
        temperature: Optional[float],
        max_tokens: int,
    ) -> None:
        event = LLMCallEvent(
            phase=phase,
            context=self._get_context(),
            system=system,
            user=user,
            raw=raw,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        with self._lock:
            self._llm_calls.append(event)

    def record_validation(
        self,
        *,
        attempt: int,
        submitted: str,
        success: bool,
        diagnostics: List[Tuple[str, str]],
        raw_diagnostic: str = "",
        context: Optional[tuple] = None,
    ) -> None:
        event = ValidationEvent(
            context=context if context is not None else self._get_context(),
            attempt=attempt,
            submitted=submitted,
            success=success,
            diagnostics=diagnostics,
            raw_diagnostic=raw_diagnostic,
        )
        with self._lock:
            self._validations.append(event)

    def record_chunk_summary(
        self,
        *,
        context: tuple,
        items_generated: int,
        corrections: int,
        success: bool,
    ) -> None:
        summary = ChunkSummary(
            context=context,
            items_generated=items_generated,
            corrections=corrections,
            success=success,
        )
        with self._lock:
            self._chunk_summaries.append(summary)

    # -- renderização --------------------------------------------------------

    def render_markdown(self) -> str:
        """Produz o relatório Markdown final, em ordem cronológica de chunk."""
        with self._lock:
            parts: List[str] = []
            parts.append(self._render_header())

            source_calls = [c for c in self._llm_calls if c.phase == "source"]
            if source_calls:
                parts.append(self._render_source_section(source_calls[0]))

            parts.append(f"## {self.coding_step_title}\n")

            unit_indices = sorted(
                {
                    c.context[1]
                    for c in self._llm_calls
                    if c.context and c.context[0] == self.unit_type
                }
            )
            for idx in unit_indices:
                parts.append(self._render_chunk_section(idx))

            parts.append(self._render_footer())
            return "\n".join(parts).rstrip() + "\n"

    def write(self, path: Path) -> None:
        """Persiste o relatório (sobrescreve se já existir)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_markdown(), encoding="utf-8")

    # -- helpers de renderização --------------------------------------------

    def _render_header(self) -> str:
        h = self._header
        lines = ["# Relatório de Depuração — synesis-coder", ""]
        if h.get("project"):
            lines.append(f"**Projeto:** {h['project']}")
        if h.get("input_name"):
            chars = h.get("input_chars")
            extra = f" ({chars:,} caracteres)".replace(",", ".") if chars else ""
            doc_label = "Corpus" if self.unit_type == "entry" else "Documento"
            lines.append(f"**{doc_label}:** {h['input_name']}{extra}")
        if h.get("bibref"):
            lines.append(f"**Referência:** @{h['bibref']}")
        if h.get("model"):
            backend = h.get("backend", "")
            backend_str = f" (backend: {backend})" if backend else ""
            lines.append(f"**Modelo:** {h['model']}{backend_str}")
        if h.get("start"):
            lines.append(f"**Início:** {h['start']}")
        lines.append("")
        if h.get("total_chunks") is not None:
            if h.get("chunk_size") is not None:
                lines.append(
                    f"**Configuração de chunking:** {h['total_chunks']} trechos · "
                    f"tamanho {h.get('chunk_size', '?')} · "
                    f"sobreposição {h.get('overlap', '?')}"
                )
            else:
                lines.append(
                    f"**Entradas:** {h['total_chunks']} referência(s) com abstract"
                )
        params = []
        if h.get("temperature") is not None:
            params.append(f"temperatura {h['temperature']}")
        if h.get("max_tokens"):
            params.append(f"máx. tokens {h['max_tokens']}")
        if params:
            lines.append("**Parâmetros do modelo:** " + " · ".join(params))
        lines.append("\n---\n")
        return "\n".join(lines)

    def _render_source_section(self, call: LLMCallEvent) -> str:
        lines = [
            "## Etapa 1 — Geração do bloco SOURCE\n",
            "> Antes de codificar, a IA cria o cabeçalho bibliográfico do documento.\n",
            "**Enviado ao modelo (instrução de sistema):**\n",
            _blockquote(call.system),
            "",
            "**Enviado (mensagem do usuário):**\n",
            _blockquote(call.user),
            "",
            f"**Resposta da IA** *(latência: {_ms(call.latency_ms)})*:\n",
            _code_block(call.raw, "synesis"),
            "",
            "---\n",
        ]
        return "\n".join(lines)

    def _render_chunk_section(self, idx: int) -> str:
        gen_call = next(
            (
                c
                for c in self._llm_calls
                if c.phase == self.unit_type and c.context and c.context[1] == idx
            ),
            None,
        )
        validations = sorted(
            (
                v
                for v in self._validations
                if v.context
                and v.context[0] == self.unit_type
                and v.context[1] == idx
            ),
            key=lambda v: v.attempt,
        )
        fix_calls = sorted(
            (
                c
                for c in self._llm_calls
                if c.phase == "fix" and c.context and c.context[1] == idx
            ),
            key=lambda c: c.latency_ms,  # ordem aproximada; rótulo usa índice
        )
        ctx = gen_call.context if gen_call and gen_call.context is not None else None
        total = ctx[2] if ctx and len(ctx) > 2 else "?"
        # Contexto de abstract carrega o bibref em context[3] — exibe junto ao título.
        bibref = ctx[3] if ctx and len(ctx) > 3 else None
        suffix = f" — @{bibref}" if bibref else ""

        lines = [f"### {self.unit_label} {idx + 1} de {total}{suffix}\n"]

        if gen_call:
            lines.append("**Instrução de sistema enviada (com suas GUIDELINES):**\n")
            lines.append(_blockquote(gen_call.system))
            lines.append("")
            lines.append("**Mensagem do usuário enviada:**\n")
            lines.append(_blockquote(gen_call.user))
            lines.append("")
            lines.append(
                f"**Resposta bruta da IA** *(latência: {_ms(gen_call.latency_ms)} · "
                f"tokens: entrada {gen_call.input_tokens:,} · "
                f"saída {gen_call.output_tokens:,})*:\n".replace(",", ".")
            )
            lines.append(_code_block(gen_call.raw, "synesis"))
            lines.append("")

        lines.append("#### Verificação pelo compilador Synesis\n")

        fix_i = 0
        corrections = 0
        for v in validations:
            if v.success:
                lines.append(
                    f"✅ **Tentativa {v.attempt + 1} — validação bem-sucedida.** "
                    "O ITEM é válido.\n"
                )
                continue

            problems = v.diagnostics
            n = len(problems) if problems else 1
            plural = "problema encontrado" if n == 1 else "problemas encontrados"
            lines.append(f"🔴 **Tentativa {v.attempt + 1} — {n} {plural}:**\n")
            if problems:
                for _cat, friendly in problems:
                    lines.append(f"- {friendly}")
            elif v.raw_diagnostic:
                lines.append(
                    "- A IA produziu texto que não segue a sintaxe Synesis "
                    "(possivelmente markdown ou bloco incompleto)."
                )
            lines.append("")

            # Correção correspondente a esta tentativa, se houver.
            if fix_i < len(fix_calls):
                fc = fix_calls[fix_i]
                fix_i += 1
                corrections += 1
                lines.append(
                    f"> A IA será solicitada a corrigir "
                    f"(temperatura {fc.temperature})."
                )
                lines.append("")
                lines.append(
                    f"**Resposta da correção** *(latência: {_ms(fc.latency_ms)})*:\n"
                )
                lines.append(_code_block(fc.raw, "synesis"))
                lines.append("")

        summary = next(
            (s for s in self._chunk_summaries if s.context[1] == idx), None
        )
        if summary:
            lines.append(
                f"**Resultado desta {self.unit_label.lower()}:** "
                f"{summary.items_generated} ITEM(s) "
                f"gerado(s) · {summary.corrections} ciclo(s) de correção.\n"
            )
        lines.append("---\n")
        return "\n".join(lines)

    def _render_footer(self) -> str:
        f = self._footer
        lines = ["## Resumo da sessão\n", "| Métrica | Valor |", "|---|---|"]
        if f.get("total_chunks") is not None:
            unit_plural = (
                "Referências processadas"
                if self.unit_type == "entry"
                else "Trechos processados"
            )
            lines.append(
                f"| {unit_plural} | {f['total_chunks']} "
                f"(OK: {f.get('total_ok', '?')}, falhas: {f.get('total_fail', '?')}) |"
            )
        if f.get("items_generated") is not None:
            lines.append(f"| ITEMs gerados | {f['items_generated']} |")
        if f.get("items_dedup") is not None:
            lines.append(f"| ITEMs após deduplicação | {f['items_dedup']} |")
        if f.get("tokens_line"):
            lines.append(f"| Tokens | {f['tokens_line']} |")
        if f.get("elapsed") is not None:
            lines.append(f"| Tempo total | {f['elapsed']:.1f} s |")
        if f.get("validation"):
            lines.append(f"| **Validação final** | {f['validation']} |")
        lines.append("")
        if f.get("output_file"):
            lines.append(f"**Arquivo gerado:** `{f['output_file']}`")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utilitários de formatação Markdown
# ---------------------------------------------------------------------------


def _blockquote(text: str) -> str:
    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _code_block(text: str, lang: str = "") -> str:
    body = (text or "").strip()
    return f"```{lang}\n{body}\n```"


def _ms(value: float) -> str:
    return f"{value:,.0f} ms".replace(",", ".")


def now_human() -> str:
    """Timestamp legível: DD/MM/AAAA HH:MM:SS."""
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")
