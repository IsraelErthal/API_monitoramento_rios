from datetime import datetime, timedelta, timezone

import pytest

from app.config import Rules
from app.processing import (Point, classify, delta, is_jump, is_stuck, max_level, moving_avg,
                            rate_m_h, trend)

R = Rules()
T = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
COTAS = (2.5, 3.0, 3.5)


def pts(*pairs):
    """pares (minutos_atrás, nível), em ordem cronológica"""
    return [Point(T - timedelta(minutes=a), n) for a, n in pairs]


def test_deltas_use_reading_with_matching_age():
    hist = pts((60, 1.0), (45, 1.1), (30, 1.2), (15, 1.3), (5, 1.35))
    assert delta(hist, T, 1.4, 5) == pytest.approx(0.05)
    assert delta(hist, T, 1.4, 15) == pytest.approx(0.10)
    assert delta(hist, T, 1.4, 60) == pytest.approx(0.40)
    assert delta(hist, T, 1.4, 180) is None          # não há leitura de 3 h atrás


def test_moving_average_and_max():
    hist = pts((200, 9.0), (50, 1.0), (30, 2.0), (10, 3.0))
    assert moving_avg(hist, T, 4.0) == pytest.approx(2.5)          # só os últimos 60 min
    assert max_level(hist, T, 4.0, 180) == 4.0
    assert max_level(hist, T, 0.5, 180) == 3.0                     # a leitura de 200 min fica de fora


def test_rate_is_slope_in_m_per_hour():
    hist = pts(*[(60 - 10 * i, 1.0 + 0.1 * i) for i in range(6)])  # 0,6 m/h
    assert rate_m_h(hist, T, 1.6) == pytest.approx(0.6)


def test_rate_needs_enough_points_and_span():
    assert rate_m_h(pts((10, 1.0)), T, 1.1) is None                # só 2 pontos
    assert rate_m_h(pts((8, 1.0), (4, 1.0)), T, 1.0) is None       # 8 min de intervalo, pouco


def test_trend_thresholds():
    assert trend(None, 0.05) is None
    assert trend(0.20, 0.05) == "rising"
    assert trend(-0.20, 0.05) == "falling"
    assert trend(0.01, 0.05) == "steady"


@pytest.mark.parametrize("level,rate,expected", [
    (1.0, 0.0, "normal"), (2.6, 0.0, "attention"), (3.1, 0.0, "alert"), (3.6, 0.0, "critical"),
    (1.0, 0.5, "attention"),                     # subida rápida sozinha já é atenção
    (3.1, 0.5, "alert"),                         # a subida não rebaixa o risco por nível
])
def test_classify_levels(level, rate, expected):
    assert classify(level, rate, COTAS, "normal", R)[0] == expected


def test_classify_without_cotas_is_unknown():
    assert classify(5.0, 1.0, (None, None, None), "normal", R)[0] == "unknown"


def test_hysteresis_holds_then_releases():
    assert classify(3.48, 0.0, COTAS, "critical", R)[0] == "critical"    # ainda dentro da folga de 0,05 m
    assert classify(3.40, 0.0, COTAS, "critical", R)[0] == "alert"
    assert classify(2.48, 0.0, COTAS, "attention", R)[0] == "attention"
    assert classify(2.40, 0.0, COTAS, "attention", R)[0] == "normal"
    assert classify(2.40, 0.2, COTAS, "attention", R)[0] == "attention"  # ainda subindo bastante


def test_reasons_are_human_readable():
    risk, reasons = classify(3.6, 0.4, COTAS, "normal", R)
    assert risk == "critical" and "inundação" in reasons[0] and "3,60 m" in reasons[0]


def test_jump_detection_and_confirmation():
    baseline = Point(T - timedelta(minutes=10), 1.0)
    assert is_jump(baseline, baseline, False, T, 6.0, R) is True                        # 30 m/h
    assert is_jump(baseline, baseline, False, T, 1.2, R) is False                       # 1,2 m/h
    suspect = Point(T - timedelta(minutes=5), 6.0)
    assert is_jump(baseline, suspect, True, T, 6.05, R) is False                        # confirmada
    assert is_jump(baseline, suspect, True, T, 1.0, R) is False                         # era um pico isolado
    assert is_jump(None, None, False, T, 99.0, R) is False                              # sem referência


def test_stuck_detection():
    flat = pts(*[(180 - 30 * i, 1.0) for i in range(6)])
    assert is_stuck(flat, T, 1.0, R) is True
    varied = pts(*[(180 - 30 * i, 1.0 + (0.01 if i == 3 else 0)) for i in range(6)])
    assert is_stuck(varied, T, 1.0, R) is False
    assert is_stuck(pts((60, 1.0), (30, 1.0)), T, 1.0, R) is False                      # poucas leituras
