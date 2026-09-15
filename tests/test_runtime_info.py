"""Testes do banner de runtime (runtime_info) — sem LLM real.

Cobre: presença das 4 informações (versão coder, versão compilador, backend/modelo,
caminho), escolha do rótulo de caminho conforme supports_json_schema(), dica
acionável só no backend anthropic em texto-livre, e emissão via logger.info.
"""

from __future__ import annotations

import logging

from synesis_coder import runtime_info
from synesis_coder.runtime_info import (
    build_banner_line,
    build_engine_banner,
    campaign_summary,
    engine_warnings,
    format_duration,
    runtime_banner,
    short_model_name,
    warn_prompt_too_large,
)


class _FakeClient:
    """Stub mínimo com a superfície que o banner consome."""

    def __init__(self, backend: str, model: str, json_schema: bool) -> None:
        self.backend = backend
        self.model = model
        self._json = json_schema

    def supports_json_schema(self) -> bool:
        return self._json


def test_banner_contains_backend_model_and_path():
    line = build_banner_line(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert "anthropic/claude-opus-4-6" in line
    # O rótulo do caminho (JSON assembler | texto-livre) sempre está presente.
    assert "texto-livre" in line or "JSON assembler" in line


def test_label_free_text_when_no_json_schema():
    line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "texto-livre (regex)" in line
    assert "JSON assembler" not in line


def test_label_json_assembler_when_supported():
    line = build_banner_line(_FakeClient("openai", "gemma", True))
    assert "JSON assembler" in line
    assert "texto-livre" not in line


def test_label_json_assembler_for_anthropic_with_structured_outputs():
    # Anthropic com SDK que suporta structured outputs: caminho JSON, sem dica.
    line = build_banner_line(_FakeClient("anthropic", "claude-sonnet-5", True))
    assert "JSON assembler" in line
    assert "SYNESIS_CODER_BACKEND=openai" not in line


def test_hint_only_for_anthropic_free_text():
    # Anthropic em texto-livre (SDK antigo): dica para atualizar o SDK.
    anthropic_line = build_banner_line(_FakeClient("anthropic", "m", False))
    assert "anthropic>=0.77.1" in anthropic_line

    # openai com json ativo: sem dica
    openai_line = build_banner_line(_FakeClient("openai", "m", True))
    assert "anthropic>=0.77.1" not in openai_line


def test_no_hint_for_anthropic_with_json():
    # Anthropic com structured outputs disponível: nenhuma dica de atualização.
    line = build_banner_line(_FakeClient("anthropic", "claude-sonnet-5", True))
    assert "anthropic>=0.77.1" not in line


def test_no_hint_for_non_anthropic_without_json():
    # Backend openai-compat que (hipoteticamente) não suporta json_schema:
    # a dica de atualizar o SDK anthropic não se aplica.
    line = build_banner_line(_FakeClient("openai", "m", False))
    assert "anthropic>=0.77.1" not in line


def test_runtime_banner_emits_via_logger(caplog):
    # O banner descreve o motor em bloco (Fase 5); antes era a linha "Motor:".
    with caplog.at_level(logging.INFO, logger=runtime_info.__name__):
        runtime_banner(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert any("Modelo" in rec.message for rec in caplog.records)


def test_runtime_banner_keeps_sdk_hint_for_anthropic_free_text(caplog):
    with caplog.at_level(logging.INFO, logger=runtime_info.__name__):
        runtime_banner(_FakeClient("anthropic", "claude-opus-4-6", False))
    assert any("anthropic>=0.77.1" in rec.message for rec in caplog.records)


def test_runtime_banner_never_fails_on_metadata_error(caplog, monkeypatch):
    # Garantia central da Fase 5: metadado indisponível encolhe o banner,
    # jamais derruba a campanha.
    from synesis_coder import model_facts

    monkeypatch.setattr(
        model_facts, "fetch_model_facts",
        lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with caplog.at_level(logging.INFO, logger=runtime_info.__name__):
        try:
            runtime_banner(_FakeClient("openai", "m", True))
        except RuntimeError:
            raise AssertionError("banner deixou a exceção escapar")


# ---------------------------------------------------------------------------
# Sumário final de campanha
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, **kw):
        self.api_calls = kw.get("api_calls", 10)
        self.corrections = kw.get("corrections", 0)
        self.schema_fallbacks = kw.get("schema_fallbacks", 0)
        self.total_prompt_tokens = kw.get("total_prompt_tokens", 1000)
        self.output_tokens = kw.get("output_tokens", 500)
        self.cache_read_tokens = kw.get("cache_read_tokens", 0)


class _FakeUsageClient:
    def __init__(self, model="claude-sonnet-5", **kw):
        self.model = model
        self.usage = _FakeUsage(**kw)


def test_format_duration_scales():
    # Segundos crus ("1005.0s") obrigam o leitor a converter; a saída escala.
    assert format_duration(42) == "42s"
    assert format_duration(1005) == "16m45s"
    assert format_duration(8000) == "2h13m"
    assert format_duration(-5) == "0s"


def test_short_model_name_strips_repo_and_quantization_path():
    # ID de modelo local ocupa a linha inteira sem informar mais que o nome.
    raw = "hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:Qwen3.8-27B-GSQ-RCO-IQ3_S"
    assert short_model_name(raw) == "Qwen3.8-27B-GSQ-RCO-IQ3_S"
    # IDs simples passam intactos.
    assert short_model_name("claude-sonnet-5") == "claude-sonnet-5"
    assert short_model_name("openai/gpt-5.6-luna") == "gpt-5.6-luna"


def test_campaign_summary_has_core_facts():
    out = campaign_summary(
        mode="abstract", project="face85", total=25, ok=23, failed=2,
        elapsed=1005.0, output="out/", unit="referências",
    )
    assert "abstract · face85" in out
    assert "25 total" in out and "23 OK (92%)" in out and "2 falhas" in out
    assert "16m45s" in out          # duração legível, não "1005.0s"
    assert "out/" in out


def test_campaign_summary_without_client_omits_token_blocks():
    # Sem llm_client o sumário degrada: contagem e tempo, sem linhas vazias
    # de tokens/tentativas (nunca "desconhecido").
    out = campaign_summary(
        mode="dataset", project="lattes", total=3, ok=3, failed=0,
        elapsed=45.0, output="annotations/",
    )
    assert "Tentativas" not in out
    assert "Tokens" not in out
    assert "Modelo" not in out
    assert "45s" in out


def test_campaign_summary_reports_corrections_and_fallbacks():
    # A linha Tentativas é a que revela custo invisível: chamadas > total.
    out = campaign_summary(
        mode="abstract", project="p", total=3, ok=3, failed=0,
        elapsed=60.0, output="o/",
        llm_client=_FakeUsageClient(api_calls=9, corrections=3,
                                    schema_fallbacks=2),
    )
    assert "chamadas 9" in out
    assert "correções 3" in out
    assert "fallbacks 2" in out


def test_campaign_summary_hides_zero_corrections_and_fallbacks():
    # Campanha limpa não carrega ruído "correções 0 · fallbacks 0".
    out = campaign_summary(
        mode="abstract", project="p", total=3, ok=3, failed=0,
        elapsed=60.0, output="o/",
        llm_client=_FakeUsageClient(api_calls=3),
    )
    assert "chamadas 3" in out
    assert "correções" not in out
    assert "fallbacks" not in out


def test_campaign_summary_thousands_separator_is_pt_br():
    out = campaign_summary(
        mode="abstract", project="p", total=1, ok=1, failed=0,
        elapsed=10.0, output="o/",
        llm_client=_FakeUsageClient(total_prompt_tokens=412300,
                                    output_tokens=88140),
    )
    assert "412.300" in out
    assert "412,300" not in out


def test_campaign_summary_skipped_only_when_resuming():
    base = dict(mode="abstract", project="p", total=5, ok=5, failed=0,
                elapsed=10.0, output="o/")
    assert "Retomadas" not in campaign_summary(**base)
    assert "Retomadas   4" in campaign_summary(**base, skipped=4)


def test_campaign_summary_zero_total_does_not_divide_by_zero():
    # Corpus vazio ou tudo pulado por --resume.
    out = campaign_summary(
        mode="abstract", project="p", total=0, ok=0, failed=0,
        elapsed=0.0, output="o/", llm_client=_FakeUsageClient(),
    )
    assert "0 total" in out


# ---------------------------------------------------------------------------
# Banner de motor (Fase 5)
# ---------------------------------------------------------------------------


def test_engine_banner_shows_everything_a_local_model_knows():
    from synesis_coder.model_facts import ModelFacts

    facts = ModelFacts(
        display_name="Qwen3.8-27B", context_window=262144, context_served=16384,
        parameters="26.9B", quantization="IQ3_S", host="192.168.1.47",
        source="Ollama",
    )
    out = build_engine_banner(facts, concurrent=1, json_path=True, thinking_off=True)
    assert "26.9B parâmetros" in out
    assert "IQ3_S" in out
    assert "16.384 de 262.144" in out
    assert "192.168.1.47" in out
    assert "concorrência 1" in out
    assert "raciocínio desativado" in out


def test_engine_banner_omits_absent_axes_never_prints_unknown():
    # API hospedada não tem parâmetros nem quantização — a pergunta não se
    # aplica. Omitir, nunca escrever "desconhecido".
    from synesis_coder.model_facts import ModelFacts

    out = build_engine_banner(
        ModelFacts(display_name="Claude Sonnet 5", context_window=200000),
        concurrent=5,
    )
    assert "Servidor" not in out
    assert "parâmetros" not in out
    assert "desconhecido" not in out.lower()
    assert "janela 200.000" in out


def test_engine_banner_minimal_facts_still_renders():
    # OpenAI informa só o id: o banner encolhe, não quebra.
    from synesis_coder.model_facts import ModelFacts

    out = build_engine_banner(ModelFacts(display_name="gpt-5.6-luna"))
    assert "gpt-5.6-luna" in out
    assert "extração JSON" in out


def test_engine_banner_does_not_repeat_quantization_in_name():
    from synesis_coder.model_facts import ModelFacts

    out = build_engine_banner(
        ModelFacts(display_name="Qwen3-27B-IQ3_S", quantization="IQ3_S")
    )
    assert out.count("IQ3_S") == 1


def test_warns_when_server_serves_a_fraction_of_the_window():
    # O incidente real: 4.096 de 262.144.
    from synesis_coder.model_facts import ModelFacts

    warnings = engine_warnings(
        ModelFacts("m", context_window=262144, context_served=4096, host="h")
    )
    assert any("OLLAMA_CONTEXT_LENGTH" in w for w in warnings)


def test_warns_when_concurrency_high_against_local_server():
    from synesis_coder.model_facts import ModelFacts

    warnings = engine_warnings(ModelFacts("m", host="192.168.1.47"), concurrent=5)
    assert any("--concurrent 1" in w for w in warnings)


def test_no_concurrency_warning_for_hosted_api():
    # Concorrência alta é correta contra API que atende em paralelo.
    from synesis_coder.model_facts import ModelFacts

    assert engine_warnings(ModelFacts("m", context_window=200000), concurrent=5) == []


def test_no_warnings_when_configuration_is_sound():
    from synesis_coder.model_facts import ModelFacts

    facts = ModelFacts("m", context_window=262144, context_served=131072, host="h")
    assert engine_warnings(facts, concurrent=1) == []


def test_prompt_too_large_warns_before_the_call(caplog):
    # O prompt é montado localmente: dá para avisar ANTES de pagar a chamada.
    from synesis_coder.model_facts import ModelFacts

    facts = ModelFacts("m", context_window=262144, context_served=4096)
    with caplog.at_level(logging.WARNING, logger=runtime_info.__name__):
        fired = warn_prompt_too_large(facts, estimated_tokens=8353)
    assert fired
    assert "8.353" in caplog.text and "4.096" in caplog.text


def test_prompt_within_window_is_silent(caplog):
    from synesis_coder.model_facts import ModelFacts

    facts = ModelFacts("m", context_served=16384)
    with caplog.at_level(logging.WARNING, logger=runtime_info.__name__):
        fired = warn_prompt_too_large(facts, estimated_tokens=8353)
    assert not fired
    assert not caplog.records


def test_prompt_check_silent_without_known_window():
    from synesis_coder.model_facts import ModelFacts

    assert not warn_prompt_too_large(ModelFacts("m"), estimated_tokens=999999)


def test_modes_do_not_log_the_summary_they_return():
    # O sumário é o valor de RETORNO do modo, impresso pela CLI com click.echo.
    # Emiti-lo também por logger.info o mostra DUAS vezes — regressão observada
    # em execução real ao promover o nível de debug para info.
    import re
    from pathlib import Path

    offenders = []
    for path in Path("synesis_coder/modes").glob("*_mode.py"):
        src = path.read_text(encoding="utf-8")
        if not re.search(r"^\s*return summary\s*$", src, re.M):
            continue
        if re.search(r"^\s*(logger|_log)\.info\(summary\)\s*$", src, re.M):
            offenders.append(path.name)

    assert not offenders, (
        f"Estes modos logam E retornam o sumário (sairia duplicado): {offenders}"
    )


def test_campaign_summary_survives_mock_client():
    # Os modos são testados com MagicMock como cliente: getattr devolve outro
    # MagicMock, que não formata como número. O sumário deve degradar, nunca
    # derrubar a execução (mesmo motivo de llm_client._int_attr).
    from unittest.mock import MagicMock

    out = campaign_summary(
        mode="ontology", project="p", total=1, ok=1, failed=0,
        elapsed=5.0, output="o.syno", llm_client=MagicMock(),
    )
    assert "chamadas 0" in out
    assert "5s" in out


def test_campaign_summary_accepts_mode_specific_extra_lines():
    out = campaign_summary(
        mode="critique", project="p", total=10, ok=10, failed=0,
        elapsed=30.0, output="o.synr", extra=[("Concordância", "0.722")],
    )
    assert "Concordância" in out and "0.722" in out
