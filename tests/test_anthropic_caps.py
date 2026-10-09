"""Capacidades por modelo Claude e a tradução do pedido do coder (sem rede).

Defeito de origem (2026-10-09): todo Claude 5.x falhava no coder. A lista de
omissão de `temperature` só tinha `claude-opus-4-7`, então Sonnet/Haiku/Opus 5.5
recebiam `temperature=0.0` e respondiam HTTP 400 — no caminho JSON e no texto
livre. Além disso, SEM campo `thinking` os 5.5 raciocinam por padrão, e o Opus
5.5 não desliga: o raciocínio consome o max_tokens da resposta.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from synesis_coder import anthropic_caps as ac
from synesis_coder.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _limpa_estado(monkeypatch):
    ac._api_cache.clear()
    ac._warned.clear()
    for v in (
        "SYNESIS_CODER_EFFORT",
        "SYNESIS_CODER_THINKING_BUDGET",
        "SYNESIS_CODER_THINKING_RESERVE",
        "SYNESIS_CODER_MAX_TOKENS",
    ):
        monkeypatch.delenv(v, raising=False)
    yield
    ac._api_cache.clear()


def _plan(model, want=False, budget=0, effort=None, max_tokens=4000, temperature=None):
    return ac.plan_request(
        model,
        ac.table_caps(model),
        want_thinking=want,
        budget=budget,
        temperature=temperature,
        max_tokens=max_tokens,
        effort=effort,
        reserve=8000,
    )


# ---------------------------------------------------------------------------
# Tabela
# ---------------------------------------------------------------------------


class TestTabela:
    def test_prefixo_mais_longo_vence(self):
        assert ac.table_caps("claude-opus-5-5").off_type is None  # não desliga
        assert ac.table_caps("claude-opus-5").off_type == "disabled"  # Opus 5 desliga
        assert ac.table_caps("claude-opus-5-5-20261001").off_type is None

    @pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"])
    def test_geracao_5_5_sem_temperature_e_raciocinio_ligado(self, model):
        caps = ac.table_caps(model)
        assert caps.temperature is False
        assert caps.thinking_default_on is True
        assert "enabled" not in caps.thinking_types

    def test_modelos_antigos_preservam_o_comportamento(self):
        caps = ac.table_caps("claude-sonnet-4-6")
        assert caps.temperature is True and caps.thinking_default_on is False
        assert "enabled" in caps.thinking_types
        assert ac.table_caps("claude-opus-4-7").temperature is False

    def test_modelo_desconhecido_e_conservador(self):
        caps = ac.table_caps("modelo-x")
        assert caps.temperature is True and not caps.thinking_types and caps.source == "padrão"


# ---------------------------------------------------------------------------
# Plano da requisição: pedido → enviado
# ---------------------------------------------------------------------------


class TestPlano:
    def test_sonnet_4_6_sem_raciocinio_envia_temperature(self):
        p = _plan("claude-sonnet-4-6")
        assert p.params == {"temperature": 0.0} and p.max_tokens == 4000 and not p.thinks

    def test_sonnet_4_6_com_orcamento_usa_extended_thinking(self):
        p = _plan("claude-sonnet-4-6", want=True, budget=5000)
        assert p.params["thinking"] == {"type": "enabled", "budget_tokens": 5000}
        assert p.params["temperature"] == 1.0
        assert p.max_tokens == 5000 + 4096

    def test_sonnet_5_5_desliga_por_between_tools_sem_temperature(self):
        p = _plan("claude-sonnet-5-5")
        assert p.params == {"thinking": {"type": "between_tools"}}
        assert "temperature" not in p.params and not p.thinks and p.max_tokens == 4000

    def test_haiku_5_5_desliga_por_disabled(self):
        assert _plan("claude-haiku-5-5").params == {"thinking": {"type": "disabled"}}

    def test_haiku_5_5_nao_desliga_com_esforco_acima_de_high(self):
        p = _plan("claude-haiku-5-5", effort="xhigh")
        assert "thinking" not in p.params and p.thinks
        assert p.params["output_config"] == {"effort": "xhigh"}
        assert p.max_tokens == 4000 + 8000
        assert "não desliga com esforço xhigh" in p.thinking_label

    def test_opus_5_5_nao_desliga_e_recebe_reserva(self, caplog):
        p = _plan("claude-opus-5-5")
        assert "thinking" not in p.params and "temperature" not in p.params
        assert p.thinks and p.max_tokens == 4000 + 8000
        assert p.thinking_label == "raciocínio adaptativo (não desliga neste modelo)"
        assert any("raciocina por padrão" in r.message for r in caplog.records)

    def test_orcamento_em_modelo_so_adaptativo_vira_adaptativo_com_aviso(self, caplog):
        p = _plan("claude-sonnet-5-5", want=True, budget=5000)
        assert p.params["thinking"] == {"type": "adaptive"}
        assert any("não aceita orçamento" in r.message for r in caplog.records)

    def test_reserva_respeita_o_teto_de_saida(self):
        p = _plan("claude-opus-5-5", max_tokens=125000)
        assert p.max_tokens == 128000

    def test_esforco_ignorado_em_modelo_sem_esforco(self):
        p = _plan("claude-sonnet-4-5", effort="low")
        assert "output_config" not in p.params

    def test_esforco_invalido_e_ignorado(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_EFFORT", "turbo")
        assert ac.requested_effort() is None
        monkeypatch.setenv("SYNESIS_CODER_EFFORT", "LOW")
        assert ac.requested_effort() == "low"


# ---------------------------------------------------------------------------
# Models API (refinamento opcional)
# ---------------------------------------------------------------------------


class TestModelsApi:
    def _client(self, capabilities):
        c = MagicMock()
        c.models.retrieve.return_value.model_dump.return_value = {
            "capabilities": capabilities,
            "max_tokens": 64000,
        }
        return c

    def test_refina_tipos_e_teto(self):
        caps = ac.resolve_caps(
            "claude-sonnet-5-5",
            self._client(
                {
                    "thinking": {
                        "types": {
                            "adaptive": {"supported": True},
                            "enabled": {"supported": False},
                            "disabled": {"supported": False},
                        }
                    },
                    "effort": {"supported": True},
                }
            ),
        )
        assert caps.max_output == 64000 and caps.source == "tabela+api"
        assert caps.off_type == "between_tools"  # a API não informa; a tabela mantém

    def test_modelo_desconhecido_so_adaptativo_e_tratado_como_geracao_5(self):
        caps = ac.resolve_caps(
            "claude-nova-6",
            self._client(
                {
                    "thinking": {
                        "types": {
                            "adaptive": {"supported": True},
                            "enabled": {"supported": False},
                            "disabled": {"supported": True},
                        }
                    }
                }
            ),
        )
        assert caps.thinking_default_on and not caps.temperature and caps.off_type == "disabled"

    def test_falha_da_api_mantem_a_tabela(self):
        c = MagicMock()
        c.models.retrieve.side_effect = RuntimeError("rede")
        assert ac.resolve_caps("claude-sonnet-5-5", c) == ac.table_caps("claude-sonnet-5-5")

    def test_cliente_simulado_sem_dict_e_ignorado(self):
        assert ac.resolve_caps("claude-sonnet-4-6", MagicMock()) == ac.table_caps(
            "claude-sonnet-4-6"
        )


# ---------------------------------------------------------------------------
# 400 de parâmetro não suportado
# ---------------------------------------------------------------------------


def _erro(status, message, tipo="invalid_request_error"):
    e = Exception(message)
    e.status_code = status
    e.body = {"type": "error", "error": {"type": tipo, "message": message}}
    return e


class TestErroDeParametro:
    def test_reconhece_temperature(self):
        assert ac.unsupported_param(_erro(400, "`temperature` is deprecated for this model.")) == (
            "temperature",
            "",
        )

    def test_reconhece_tipo_de_thinking(self):
        e = _erro(400, '"thinking.type.disabled" is not supported for this model. Use ...')
        assert ac.unsupported_param(e) == ("thinking_type", "disabled")

    @pytest.mark.parametrize(
        "erro",
        [
            _erro(400, "max_tokens: temperature budget exceeded"),  # cita a palavra, outro erro
            _erro(400, "messages: roles must alternate"),
            _erro(429, "`temperature` is deprecated for this model."),  # não é 400
            _erro(400, "`temperature` is deprecated", tipo="authentication_error"),
        ],
    )
    def test_outros_erros_nao_sao_reconhecidos(self, erro):
        assert ac.unsupported_param(erro) is None

    def test_correcao_desliga_o_parametro(self):
        caps = ac.table_caps("claude-sonnet-4-6")
        assert ac.without(caps, ("temperature", "")).temperature is False
        sem = ac.without(ac.table_caps("claude-haiku-5-5"), ("thinking_type", "disabled"))
        assert sem.off_type is None and sem.thinking_default_on


# ---------------------------------------------------------------------------
# Integração com o LLMClient (cliente simulado)
# ---------------------------------------------------------------------------


def _resp(text="ok"):
    r = MagicMock()
    b = MagicMock()
    b.type, b.text = "text", text
    r.content, r.stop_reason = [b], "end_turn"
    r.usage = MagicMock(
        input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return r


def _cliente(model):
    with patch("synesis_coder.llm_client._get_anthropic_api_key", return_value="k"):
        c = LLMClient(model=model, backend="anthropic")
    c._client = MagicMock()
    c._rate_limit_enabled = False
    return c


class TestClienteAnthropic:
    def test_sonnet_5_5_nao_envia_temperature(self):
        c = _cliente("claude-sonnet-5-5")
        c._client.messages.create.return_value = _resp()
        assert c.call([{"role": "user", "content": "x"}], max_tokens=4000) == "ok"
        kw = c._client.messages.create.call_args.kwargs
        assert "temperature" not in kw and kw["thinking"] == {"type": "between_tools"}

    def test_esforco_e_schema_dividem_o_output_config(self, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_EFFORT", "low")
        c = _cliente("claude-sonnet-5-5")
        c._client.messages.create.return_value = _resp('{"a": 1}')
        assert c.call_json(
            [{"role": "user", "content": "x"}],
            {"type": "object", "properties": {"a": {"type": "integer"}}},
            max_tokens=4000,
        ) == {"a": 1}
        oc = c._client.messages.create.call_args.kwargs["output_config"]
        assert oc["effort"] == "low" and oc["format"]["type"] == "json_schema"

    def test_400_de_temperature_repete_uma_vez_e_memoriza(self, caplog):
        c = _cliente("claude-modelo-novo")  # fora da tabela: envia temperature
        c._client.messages.create.side_effect = [
            _erro(400, "`temperature` is deprecated for this model."),
            _resp(),
            _resp(),
        ]
        assert c.call([{"role": "user", "content": "x"}], max_tokens=100) == "ok"
        segunda = c._client.messages.create.call_args_list[1].kwargs
        assert "temperature" not in segunda
        c.call([{"role": "user", "content": "y"}], max_tokens=100)
        assert c._client.messages.create.call_count == 3  # memorizado: sem nova falha
        assert "temperature" not in c._client.messages.create.call_args.kwargs
        assert any("recusou temperature" in r.message for r in caplog.records)

    def test_outro_400_nao_e_repetido_pela_correcao(self):
        c = _cliente("claude-sonnet-4-6")
        c._client.messages.create.side_effect = _erro(400, "messages: roles must alternate")
        with patch("synesis_coder.llm_client._get_max_retries", return_value=1):
            with pytest.raises(Exception, match="roles must alternate"):
                c.call([{"role": "user", "content": "x"}], max_tokens=100)
        assert c._client.messages.create.call_count == 1

    def test_banner_descreve_o_raciocinio_que_roda(self):
        assert (
            _cliente("claude-opus-5-5").thinking_label()
            == "raciocínio adaptativo (não desliga neste modelo)"
        )
        assert (
            _cliente("claude-sonnet-5-5").thinking_label()
            == "raciocínio desativado (between_tools)"
        )
        assert _cliente("claude-sonnet-4-6").thinking_label() == ""


class TestStreamingAcimaDoLimite:
    """O SDK recusa chamada não-streaming estimada em > 10 min (≈ 21.333 tokens).
    Com SYNESIS_CODER_MAX_TOKENS=16000, a reserva do Opus 5.5 (que não desliga o
    raciocínio) leva a 24.000 — falha real observada em 2026-10-09."""

    def test_max_tokens_alto_usa_streaming(self):
        c = _cliente("claude-opus-5-5")
        c._client.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = _resp()
        assert c.call([{"role": "user", "content": "x"}], max_tokens=16000) == "ok"
        kw = c._client.messages.stream.call_args.kwargs
        assert kw["max_tokens"] == 24000
        c._client.messages.create.assert_not_called()

    def test_max_tokens_baixo_continua_sem_streaming(self):
        c = _cliente("claude-opus-5-5")
        c._client.messages.create.return_value = _resp()
        c.call([{"role": "user", "content": "x"}], max_tokens=1000)
        c._client.messages.stream.assert_not_called()
