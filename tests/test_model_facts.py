"""Testes dos metadados do motor (model_facts) — sem rede real.

Cobre: registro comum entre provedores, leitura de chave por sufixo
(a arquitetura prefixa no Ollama), detecção de host local, subutilização de
contexto, e a garantia central — metadado nunca derruba a campanha.
"""

from __future__ import annotations

from unittest.mock import patch

from synesis_coder import model_facts as mf
from synesis_coder.model_facts import (
    ModelFacts,
    _is_local_host,
    _suffix_key,
    fetch_model_facts,
    short_name,
)


class _Client:
    def __init__(self, backend="openai", model="m"):
        self.backend = backend
        self.model = model


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------


def test_display_name_comes_from_gguf_metadata_not_the_tag(monkeypatch):
    # Em GGUF do Hugging Face o que vem depois do ":" é a QUANTIZAÇÃO, não o
    # modelo: a tag `...gemma-4-26B-A4B-it-GGUF:UD-Q3_K_XL` exibia
    # "UD-Q3_K_XL" como se fosse o nome.
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    show = {
        "details": {"parameter_size": "25.2B", "quantization_level": "Q3_K_M"},
        "model_info": {"general.basename": "Gemma-4-26B-A4B-It",
                       "gemma4.context_length": 262144},
    }

    def fake(url, **kw):
        return show if url.endswith("/api/show") else {"models": []}

    with patch.object(mf, "_http_json", side_effect=fake):
        facts = fetch_model_facts(
            _Client(model="hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q3_K_XL")
        )
    assert facts.display_name == "Gemma-4-26B-A4B-It"


def test_display_name_falls_back_to_tag_without_metadata(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    show = {"details": {}, "model_info": {}}

    def fake(url, **kw):
        return show if url.endswith("/api/show") else {"models": []}

    with patch.object(mf, "_http_json", side_effect=fake):
        facts = fetch_model_facts(_Client(model="qwen3:27b"))
    assert facts.display_name == "27b"  # sem metadados, resta a tag


def test_short_name_strips_repo_and_tag():
    raw = "hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:Qwen3.8-27B-GSQ-RCO-IQ3_S"
    assert short_name(raw) == "Qwen3.8-27B-GSQ-RCO-IQ3_S"
    assert short_name("claude-sonnet-5") == "claude-sonnet-5"
    assert short_name("openai/gpt-5.6-luna") == "gpt-5.6-luna"


def test_suffix_key_survives_architecture_prefix():
    # A chave de contexto do Ollama é prefixada pela ARQUITETURA, que muda de
    # modelo para modelo: ler por chave fixa quebraria no modelo seguinte.
    assert _suffix_key({"qwen35.context_length": 262144}, ".context_length") == 262144
    assert _suffix_key({"llama.context_length": 8192}, ".context_length") == 8192
    assert _suffix_key({"outra.coisa": 1}, ".context_length") is None
    assert _suffix_key({}, ".context_length") is None


def test_suffix_key_ignores_non_integer():
    assert _suffix_key({"x.context_length": "muitos"}, ".context_length") is None


def test_is_local_host_covers_private_ranges():
    assert _is_local_host("http://localhost:11434")
    assert _is_local_host("http://127.0.0.1:11434")
    assert _is_local_host("http://192.168.1.47:11434")
    assert _is_local_host("http://10.0.0.5:11434")
    assert _is_local_host("http://172.16.3.2:11434")
    assert not _is_local_host("https://openrouter.ai/api")
    assert not _is_local_host("https://api.anthropic.com")
    assert not _is_local_host("http://172.32.0.1:11434")  # fora de 16-31


# ---------------------------------------------------------------------------
# ModelFacts — propriedades derivadas
# ---------------------------------------------------------------------------


def test_detects_the_untouched_vendor_default():
    # O caso medido: servidor no default de fábrica (4.096), com o modelo
    # comportando 262.144. Não é dimensionamento, é ausência de configuração.
    f = ModelFacts("m", context_window=262144, context_served=4096)
    assert f.context_at_vendor_default


def test_any_deliberate_sizing_is_not_flagged():
    # Quanto contexto cabe depende da VRAM da máquina, que o coder NÃO conhece:
    # o KV cache cresce com o contexto e disputa memória com os pesos. Julgar
    # por fração da janela do modelo empurraria o pesquisador para uma
    # configuração que vaza para a RAM — pior que o silêncio. Só o default
    # intocado é afirmável sem conhecer o hardware.
    for served in (8192, 16384, 32768, 131072):
        f = ModelFacts("m", context_window=262144, context_served=served)
        assert not f.context_at_vendor_default, f"{served} não deve alertar"


def test_vendor_default_needs_both_numbers():
    assert not ModelFacts("m", context_window=262144).context_at_vendor_default
    assert not ModelFacts("m", context_served=4096).context_at_vendor_default
    assert not ModelFacts("m").context_at_vendor_default


def test_4096_is_not_flagged_when_it_is_the_model_maximum():
    # Modelo cuja janela É 4.096: servir isso é o teto, não descuido.
    f = ModelFacts("m", context_window=4096, context_served=4096)
    assert not f.context_at_vendor_default


def test_is_local_only_when_host_known():
    assert ModelFacts("m", host="192.168.1.47").is_local
    assert not ModelFacts("m").is_local


# ---------------------------------------------------------------------------
# fetch_model_facts — degradação
# ---------------------------------------------------------------------------


def test_network_failure_degrades_to_minimal_record(monkeypatch):
    # A garantia central: metadado é informação SOBRE a execução, nunca
    # condição DELA.
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    with patch.object(mf, "_http_json", return_value=None):
        facts = fetch_model_facts(_Client(model="hf.co/x/y:Qwen3-27B-IQ3_S"))
    assert facts.display_name == "Qwen3-27B-IQ3_S"
    assert facts.context_window is None


def test_unexpected_exception_never_escapes(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    with patch.object(mf, "_http_json", side_effect=RuntimeError("boom")):
        facts = fetch_model_facts(_Client(model="m"))
    assert facts.display_name == "m"


def test_no_api_url_returns_minimal(monkeypatch):
    monkeypatch.delenv("SYNESIS_CODER_API_URL", raising=False)
    facts = fetch_model_facts(_Client(model="m"))
    assert facts == ModelFacts(display_name="m")


def test_ollama_adapter_maps_all_fields(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    show = {
        "details": {"parameter_size": "26.9B", "quantization_level": "IQ3_S"},
        "model_info": {"qwen35.context_length": 262144},
    }
    ps = {"models": [{"name": "m", "context_length": 16384}]}

    def fake(url, **kw):
        return show if url.endswith("/api/show") else ps

    with patch.object(mf, "_http_json", side_effect=fake):
        facts = fetch_model_facts(_Client(model="m"))

    assert facts.parameters == "26.9B"
    assert facts.quantization == "IQ3_S"
    assert facts.context_window == 262144
    assert facts.context_served == 16384
    assert facts.host == "192.168.1.47"
    assert facts.source == "Ollama"


def test_ollama_without_loaded_model_has_no_served_context(monkeypatch):
    # /api/ps devolve vazio quando o Ollama descarregou o modelo por
    # inatividade — observado no servidor real.
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "http://192.168.1.47:11434")
    show = {"details": {}, "model_info": {"qwen35.context_length": 262144}}

    def fake(url, **kw):
        return show if url.endswith("/api/show") else {"models": []}

    with patch.object(mf, "_http_json", side_effect=fake):
        facts = fetch_model_facts(_Client(model="m"))

    assert facts.context_window == 262144
    assert facts.context_served is None


def test_anthropic_adapter_reads_max_input_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = {"display_name": "Claude Sonnet 5", "max_input_tokens": 200000}
    with patch.object(mf, "_http_json", return_value=payload):
        facts = fetch_model_facts(_Client(backend="anthropic", model="claude-sonnet-5"))
    assert facts.display_name == "Claude Sonnet 5"
    assert facts.context_window == 200000
    assert facts.parameters is None  # não se aplica a API hospedada
    assert not facts.is_local


def test_anthropic_without_key_degrades(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    facts = fetch_model_facts(_Client(backend="anthropic", model="claude-sonnet-5"))
    assert facts.display_name == "claude-sonnet-5"
    assert facts.context_window is None


def test_openrouter_adapter_picks_matching_model(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://openrouter.ai/api")
    payload = {"data": [
        {"id": "outro/modelo", "context_length": 8000},
        {"id": "alvo/modelo", "name": "Alvo", "context_length": 128000},
    ]}
    with patch.object(mf, "_http_json", return_value=payload):
        facts = fetch_model_facts(_Client(model="alvo/modelo"))
    assert facts.display_name == "Alvo"
    assert facts.context_window == 128000
    assert facts.source == "OpenRouter"


def test_openrouter_unknown_model_degrades(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_API_URL", "https://openrouter.ai/api")
    with patch.object(mf, "_http_json", return_value={"data": []}):
        facts = fetch_model_facts(_Client(model="nao/listado"))
    assert facts.context_window is None


def test_gemini_uses_native_endpoint(monkeypatch):
    # Medido: a rota openai-compat NÃO informa a janela; só a nativa.
    monkeypatch.setenv("SYNESIS_CODER_API_KEY", "AQ.test")
    monkeypatch.setenv(
        "SYNESIS_CODER_API_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    )
    payload = {"displayName": "Gemini 3.1 Pro", "inputTokenLimit": 1048576}
    captured = {}

    def fake(url, **kw):
        captured["url"] = url
        return payload

    with patch.object(mf, "_http_json", side_effect=fake):
        facts = fetch_model_facts(_Client(model="gemini-3.1-pro-preview"))

    assert "/v1beta/models/" in captured["url"]
    assert "openai" not in captured["url"]
    assert facts.context_window == 1048576
