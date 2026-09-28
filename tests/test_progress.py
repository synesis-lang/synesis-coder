"""Testes do indicador de progresso (progress) — sem LLM real.

Cobre: throttling por intervalo mínimo, emissão garantida da última linha,
contagem de falhas, ETA/ritmo, e segurança sob concorrência.
"""

from __future__ import annotations

import logging
import threading

from synesis_coder import progress as progress_mod
from synesis_coder.progress import BatchProgress, format_eta, format_rate


def test_format_eta_scales():
    assert format_eta(45) == "~45s"
    assert format_eta(720) == "~12min"
    assert format_eta(8000) == "~2h13m"


def test_format_rate_picks_readable_scale():
    # API rápida: unidades por segundo.
    assert format_rate(2.5, "ref") == "2.5 ref/s"
    # Backend local lento: por minuto, não "0.0 ref/s".
    assert format_rate(0.05, "ref") == "3.0 ref/min"
    # Muito lento (modelo local grande): por hora.
    assert format_rate(0.001, "ref") == "3.6 ref/h"


def test_start_announces_before_any_unit_completes(caplog):
    # Num backend lento a primeira conclusão pode levar dezenas de minutos: sem
    # anúncio de início o terminal fica indistinguível de um processo travado.
    p = BatchProgress(20, unit="ref")
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.start()
    assert len(caplog.records) == 1
    assert "20" in caplog.records[0].message
    assert "ref" in caplog.records[0].message


def test_start_is_silent_when_there_is_nothing_to_do(caplog):
    p = BatchProgress(0, unit="ref")
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.start()
    assert not caplog.records


def test_start_does_not_count_as_progress(caplog):
    # start() anuncia; não move o contador nem consome o throttling.
    p = BatchProgress(2, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.start()
        p.mark()
    assert p.done == 1
    assert "[1/2" in caplog.records[1].message


def test_all_batch_modes_announce_start():
    # Regressão: um modo que instancia BatchProgress e não chama start()
    # volta ao silêncio inicial que motivou esta correção.
    import re
    from pathlib import Path

    offenders = []
    for path in Path("synesis_coder/modes").glob("*_mode.py"):
        src = path.read_text(encoding="utf-8")
        m = re.search(r"(\w+) = BatchProgress\(", src)
        if m and f"{m.group(1)}.start()" not in src:
            offenders.append(path.name)
    assert not offenders, f"modos sem anúncio de início: {offenders}"


def test_emits_final_line_even_when_throttled(caplog):
    # Com intervalo alto só a última linha sai — mas ela SAI: é o fechamento
    # que diz ao pesquisador que a campanha terminou.
    p = BatchProgress(3, unit="ref", min_interval=999)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()
        p.mark()
        p.mark()
    assert len(caplog.records) == 1
    assert "[3/3 | 100.0%]" in caplog.records[0].message


def test_emits_every_mark_when_interval_zero(caplog):
    p = BatchProgress(3, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        for _ in range(3):
            p.mark()
    assert len(caplog.records) == 3


def test_counts_failures_with_correct_plural(caplog):
    p = BatchProgress(2, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark(success=False)
        p.mark(success=False)
    assert "1 falha " in caplog.records[0].message + " "
    assert "2 falhas" in caplog.records[1].message


def test_no_failure_segment_when_all_succeed(caplog):
    p = BatchProgress(2, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()
        p.mark()
    assert all("falha" not in r.message for r in caplog.records)


def test_detail_is_appended(caplog):
    p = BatchProgress(1, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark(detail="@smith2024")
    assert "@smith2024" in caplog.records[0].message


def test_last_line_has_no_eta(caplog):
    # ETA na linha final seria ruído: não falta nada.
    p = BatchProgress(2, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()
        p.mark()
    assert "ETA" not in caplog.records[-1].message


def test_finish_is_safe_when_nothing_processed(caplog):
    # Corpus vazio, ou tudo pulado por --resume: nada a relatar.
    p = BatchProgress(0, unit="ref")
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.finish()
    assert not caplog.records


def test_zero_total_does_not_divide_by_zero(caplog):
    p = BatchProgress(0, unit="ref", min_interval=0)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()  # mais conclusões que o total previsto
    assert caplog.records  # não estourou


def test_concurrent_marks_do_not_lose_count():
    # Os modos concluem unidades em tarefas distintas; a contagem é partilhada.
    p = BatchProgress(200, unit="ref", min_interval=999)

    def worker():
        for _ in range(50):
            p.mark()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert p.done == 200


def test_shows_corrections_and_fallbacks_during_campaign(caplog):
    # Ver fallbacks subindo aos 10 min é sinal para parar e ajustar o contexto;
    # vê-los só no sumário final é tarde numa campanha de horas.
    from synesis_coder.token_usage import TokenUsage

    usage = TokenUsage()
    p = BatchProgress(2, unit="ref", min_interval=0, usage=usage)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        usage.record(input_tok=10, output_tok=5)
        p.mark()
        usage.record_schema_fallback()
        usage.record(input_tok=10, output_tok=5, is_correction=True)
        p.mark()

    assert "fallback" not in caplog.records[0].message
    assert "1 fallback" in caplog.records[1].message
    assert "1 correção" in caplog.records[1].message


def test_clean_campaign_shows_no_signal_noise(caplog):
    from synesis_coder.token_usage import TokenUsage

    usage = TokenUsage()
    p = BatchProgress(2, unit="ref", min_interval=0, usage=usage)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        usage.record(input_tok=10, output_tok=5)
        p.mark()
        usage.record(input_tok=10, output_tok=5)
        p.mark()

    for rec in caplog.records:
        assert "correç" not in rec.message
        assert "fallback" not in rec.message
        assert "falha" not in rec.message


def test_signal_plurals_agree_with_count(caplog):
    from synesis_coder.token_usage import TokenUsage

    usage = TokenUsage()
    p = BatchProgress(3, unit="ref", min_interval=0, usage=usage)
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        usage.record(input_tok=1, output_tok=1, is_correction=True)
        p.mark(success=False)                      # 1 falha, 1 correção
        usage.record(input_tok=1, output_tok=1, is_correction=True)
        p.mark(success=False)                      # 2 falhas, 2 correções
        p.mark()

    assert "1 falha ·" in caplog.records[0].message
    assert "1 correção" in caplog.records[0].message
    assert "2 falhas" in caplog.records[1].message
    assert "2 correções" in caplog.records[1].message


def test_progress_survives_mock_usage(caplog):
    # Os modos são exercitados com MagicMock: getattr devolve MagicMock, que
    # não compara como número. Degradar, nunca derrubar a campanha relatada.
    from unittest.mock import MagicMock

    p = BatchProgress(1, unit="ref", min_interval=0, usage=MagicMock())
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()
    assert "[1/1 | 100.0%]" in caplog.records[0].message


def test_label_prefixes_line(caplog):
    p = BatchProgress(1, unit="item", min_interval=0, label="Revisando")
    with caplog.at_level(logging.INFO, logger=progress_mod.__name__):
        p.mark()
    assert caplog.records[0].message.startswith("Revisando")
