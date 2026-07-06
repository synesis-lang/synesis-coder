"""Testes para o módulo debug_log (flag --debug).

Cobre: classificação de erros, tradução de diagnósticos, acumulação de eventos
no DebugRecorder, renderização Markdown, thread-safety e o caminho no-op
(sem recorder injetado no LLMClient → zero overhead, comportamento inalterado).

Nenhum teste aqui chama o LLM.
"""

from __future__ import annotations

import threading
from pathlib import Path

from synesis_coder.debug_log import (
    DebugRecorder,
    classify_error,
    translate_diagnostics,
)

# ---------------------------------------------------------------------------
# Stubs de erro do compilador (imitam synesis.ast.results)
# ---------------------------------------------------------------------------


class _FakeError:
    """Imita um ValidationError do compilador: nome de classe + CODE + to_cli_line."""

    CODE = "SYNESIS_E000"

    def __init__(self, cli_line: str) -> None:
        self._cli = cli_line

    def to_cli_line(self) -> str:
        return self._cli


class UnknownFieldName(_FakeError):
    CODE = "SYNESIS_E022"


class InvalidChainRelation(_FakeError):
    CODE = "SYNESIS_E010"


class OrphanItem(_FakeError):
    CODE = "SYNESIS_E002"


class SomethingExotic(_FakeError):
    CODE = "SYNESIS_E999"


class _FakeResult:
    def __init__(self, errors):
        self.validation_result = type("VR", (), {"errors": errors})()


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_structural(self):
        assert classify_error(UnknownFieldName("x")) == "structural"

    def test_value(self):
        assert classify_error(InvalidChainRelation("x")) == "value"

    def test_other(self):
        assert classify_error(SomethingExotic("x")) == "other"


# ---------------------------------------------------------------------------
# translate_diagnostics
# ---------------------------------------------------------------------------


class TestTranslateDiagnostics:
    def test_translates_and_classifies(self):
        result = _FakeResult(
            [UnknownFieldName("campo inventado: relacao"),
             InvalidChainRelation("relação REDUCES inválida")]
        )
        out = translate_diagnostics(result)
        assert len(out) == 2
        assert out[0][0] == "structural"
        assert "campo inventado" in out[0][1]
        assert "SYNESIS_E022" in out[0][1]  # código técnico anexado
        assert out[1][0] == "value"

    def test_orphan_item_ignored(self):
        result = _FakeResult([OrphanItem("@x sem source"), UnknownFieldName("y")])
        out = translate_diagnostics(result)
        assert len(out) == 1
        assert out[0][0] == "structural"

    def test_no_errors_attribute_returns_empty(self):
        class NoErrors:
            pass

        assert translate_diagnostics(NoErrors()) == []


# ---------------------------------------------------------------------------
# DebugRecorder — acumulação e render
# ---------------------------------------------------------------------------


class TestDebugRecorderRender:
    def _populate(self, rec: DebugRecorder) -> None:
        rec.record_session_header(
            project="social_acceptance",
            input_name="entrevista.txt",
            input_chars=18420,
            bibref="smith2024",
            model="gemini-3.1-pro-preview",
            backend="openai",
            start="14/06/2026 09:32:07",
            total_chunks=2,
            chunk_size=12000,
            overlap=2400,
            temperature=0.0,
        )
        # SOURCE
        rec.record_llm_call(
            phase="source", system="Gere SOURCE...", user="BIBREF: @smith2024",
            raw="SOURCE @smith2024\nEND SOURCE", latency_ms=1240.0,
            input_tokens=300, output_tokens=20, model="gemini-3.1-pro-preview",
            temperature=0.0, max_tokens=4096,
        )
        # chunk 0 — válido na primeira tentativa
        rec.set_context(("chunk", 0, 2))
        rec.record_llm_call(
            phase="chunk", system="ITEM FIELDS chain (CHAIN)...",
            user="BIBREF + texto", raw="ITEM @smith2024\nEND ITEM",
            latency_ms=3870.0, input_tokens=6557, output_tokens=198,
            model="gemini-3.1-pro-preview", temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=0, submitted="ITEM ...", success=True, diagnostics=[],
            context=("chunk", 0, 2),
        )
        rec.record_chunk_summary(
            context=("chunk", 0, 2), items_generated=1, corrections=0, success=True,
        )
        # chunk 1 — erro na 1ª, corrige na 2ª
        rec.set_context(("chunk", 1, 2))
        rec.record_llm_call(
            phase="chunk", system="ITEM FIELDS...", user="BIBREF + texto2",
            raw="ITEM @smith2024\n    chain: a -> REDUCES -> b\nEND ITEM",
            latency_ms=4000.0, input_tokens=6600, output_tokens=210,
            model="gemini-3.1-pro-preview", temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=0, submitted="ITEM bad", success=False,
            diagnostics=[("value", "Relação `REDUCES` inválida *(código técnico: SYNESIS_E010)*")],
            context=("chunk", 1, 2),
        )
        rec.record_llm_call(
            phase="fix", system="", user="fix prompt",
            raw="ITEM @smith2024\n    chain: a -> INFLUENCES -> b\nEND ITEM",
            latency_ms=2100.0, input_tokens=400, output_tokens=50,
            model="gemini-3.1-pro-preview", temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=1, submitted="ITEM fixed", success=True, diagnostics=[],
            context=("chunk", 1, 2),
        )
        rec.record_chunk_summary(
            context=("chunk", 1, 2), items_generated=1, corrections=1, success=True,
        )
        rec.record_session_footer(
            total_chunks=2, total_ok=2, total_fail=0,
            items_generated=2, items_dedup=2,
            tokens_line="tokens: in 13.857 | out 478 | total 14.335 | calls 4",
            elapsed=11.2, validation="✅ OK", output_file="out.syn",
        )

    def test_render_contains_key_sections(self):
        rec = DebugRecorder()
        self._populate(rec)
        md = rec.render_markdown()

        assert "# Relatório de Depuração — synesis-coder" in md
        assert "social_acceptance" in md
        assert "@smith2024" in md
        assert "Etapa 1 — Geração do bloco SOURCE" in md
        assert "Etapa 2 — Codificação dos trechos" in md
        assert "Trecho 1 de 2" in md
        assert "Trecho 2 de 2" in md
        # Erro amigável + correção visíveis
        assert "REDUCES" in md
        assert "INFLUENCES" in md
        assert "Resumo da sessão" in md

    def test_render_marks_success_and_failure(self):
        rec = DebugRecorder()
        self._populate(rec)
        md = rec.render_markdown()
        assert "✅" in md
        assert "🔴" in md

    def test_write_creates_file(self, tmp_path: Path):
        rec = DebugRecorder()
        self._populate(rec)
        target = tmp_path / "sub" / "report_debug.md"
        rec.write(target)
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "Relatório de Depuração" in content

    def test_chunks_rendered_in_order(self):
        rec = DebugRecorder()
        self._populate(rec)
        md = rec.render_markdown()
        assert md.index("Trecho 1 de 2") < md.index("Trecho 2 de 2")

    def test_full_content_not_truncated(self):
        """Prompts/documentos longos devem aparecer na íntegra (sem reticências).

        O pesquisador precisa ver exatamente como o prompt foi montado e o que
        o documento entregou para decidir se o processamento está adequado.
        """
        rec = DebugRecorder()
        long_system = "GUIDELINE LINHA. " * 600  # ~10k chars, bem acima do limite antigo
        long_user = "TEXTO DO DOCUMENTO. " * 500
        rec.record_session_header(
            project="p", input_name="doc.txt", input_chars=10,
            bibref="x", model="m", backend="openai", start="agora",
            total_chunks=1, chunk_size=12000, overlap=2400, temperature=0.0,
        )
        rec.set_context(("chunk", 0, 1))
        rec.record_llm_call(
            phase="chunk", system=long_system, user=long_user,
            raw="ITEM @x\nEND ITEM", latency_ms=1.0,
            input_tokens=1, output_tokens=1, model="m",
            temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=0, submitted="ITEM", success=True, diagnostics=[],
            context=("chunk", 0, 1),
        )
        md = rec.render_markdown()
        assert "truncado" not in md
        # conteúdo íntegro presente (início e fim do system e do user)
        assert long_system.strip() in md.replace("> ", "")
        assert long_user.strip() in md.replace("> ", "")


# ---------------------------------------------------------------------------
# Unidade "entry" (modo abstract): rótulos e contexto com bibref
# ---------------------------------------------------------------------------


class TestEntryUnitRender:
    """O recorder do modo abstract usa unit_type='entry' e exibe o bibref."""

    def _populate(self, rec: DebugRecorder) -> None:
        rec.record_session_header(
            project="face85",
            input_name="face85.bib",
            bibref=None,
            model="gemini-3.1-pro-preview",
            backend="openai",
            start="14/06/2026 20:00:00",
            total_chunks=2,
            temperature=0.0,
        )
        # entrada 0 — válida de primeira
        rec.set_context(("entry", 0, 2, "silva2024"))
        rec.record_llm_call(
            phase="entry", system="ITEM FIELDS...", user="abstract A",
            raw="ITEM @silva2024\nEND ITEM", latency_ms=2000.0,
            input_tokens=900, output_tokens=120,
            model="gemini-3.1-pro-preview", temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=0, submitted="ITEM ...", success=True, diagnostics=[],
            context=("entry", 0, 2, "silva2024"),
        )
        rec.record_chunk_summary(
            context=("entry", 0, 2, "silva2024"),
            items_generated=1, corrections=0, success=True,
        )
        # entrada 1 — bibref distinto
        rec.set_context(("entry", 1, 2, "souza2025"))
        rec.record_llm_call(
            phase="entry", system="ITEM FIELDS...", user="abstract B",
            raw="ITEM @souza2025\nEND ITEM", latency_ms=2100.0,
            input_tokens=950, output_tokens=130,
            model="gemini-3.1-pro-preview", temperature=0.0, max_tokens=4096,
        )
        rec.record_validation(
            attempt=0, submitted="ITEM ...", success=True, diagnostics=[],
            context=("entry", 1, 2, "souza2025"),
        )
        rec.record_chunk_summary(
            context=("entry", 1, 2, "souza2025"),
            items_generated=1, corrections=0, success=True,
        )
        rec.record_session_footer(
            total_chunks=2, total_ok=2, total_fail=0,
            tokens_line="tokens: in 1.850 | out 250 | total 2.100 | calls 2",
            elapsed=4.5, validation="✅ OK", output_file="annotations.syn",
        )

    def _make(self) -> DebugRecorder:
        return DebugRecorder(
            unit_type="entry",
            unit_label="Referência",
            coding_step_title="Etapa 1 — Codificação dos abstracts",
        )

    def test_uses_entry_labels_and_bibref(self):
        rec = self._make()
        self._populate(rec)
        md = rec.render_markdown()
        assert "Etapa 1 — Codificação dos abstracts" in md
        assert "Referência 1 de 2 — @silva2024" in md
        assert "Referência 2 de 2 — @souza2025" in md
        # rótulos do cabeçalho/rodapé adaptados à unidade "entry"
        assert "**Corpus:** face85.bib" in md
        assert "referência(s) com abstract" in md
        assert "Referências processadas" in md
        # não deve usar a vocabulário de chunking do modo document
        assert "Configuração de chunking" not in md
        assert "Trecho 1" not in md

    def test_entries_rendered_in_order(self):
        rec = self._make()
        self._populate(rec)
        md = rec.render_markdown()
        assert md.index("Referência 1") < md.index("Referência 2")


# ---------------------------------------------------------------------------
# Thread-safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_records_no_loss(self):
        rec = DebugRecorder()
        n_threads = 10
        per_thread = 20

        def worker(tid: int) -> None:
            for i in range(per_thread):
                rec.set_context(("chunk", tid, n_threads))
                rec.record_llm_call(
                    phase="chunk", system="s", user="u", raw="r",
                    latency_ms=1.0, input_tokens=1, output_tokens=1,
                    model="m", temperature=0.0, max_tokens=10,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(rec._llm_calls) == n_threads * per_thread

    def test_context_is_per_thread(self):
        rec = DebugRecorder()
        seen: dict = {}

        def worker(tid: int) -> None:
            rec.set_context(("chunk", tid, 99))
            # cada thread deve ler seu próprio contexto
            seen[tid] = rec._get_context()

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tid in range(5):
            assert seen[tid] == ("chunk", tid, 99)


# ---------------------------------------------------------------------------
# No-op: LLMClient sem recorder não muda comportamento
# ---------------------------------------------------------------------------


class TestNoOpWithoutRecorder:
    def test_client_recorder_defaults_none(self):
        # Não instancia backend real: apenas verifica o default do atributo
        # via __init__ parcial seria complexo; em vez disso, confirmamos que a
        # assinatura aceita recorder=None implicitamente checando o atributo
        # após uma construção com backend openai (não faz chamada de rede).
        import os

        from synesis_coder.llm_client import LLMClient

        os.environ.setdefault("SYNESIS_CODER_BACKEND", "openai")
        os.environ.setdefault("SYNESIS_CODER_API_URL", "http://localhost:11434")
        os.environ.setdefault("SYNESIS_CODER_API_KEY", "x")
        client = LLMClient(model="dummy")
        assert client.recorder is None
