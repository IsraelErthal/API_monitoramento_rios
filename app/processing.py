"""Tratamento dos dados e regras de risco. Funções puras (sem banco), fáceis de testar."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Rules

RISK_ORDER = {"unknown": -1, "normal": 0, "attention": 1, "alert": 2, "critical": 3}
ALERT_RISKS = ("attention", "alert", "critical")
RISK_PT = {"normal": "normal", "attention": "atenção", "alert": "alerta", "critical": "crítico", "unknown": "indefinido"}


@dataclass(frozen=True)
class Point:
    ts: datetime
    level: float


def m(x: float) -> str:
    return f"{x:.2f}".replace(".", ",") + " m"


def delta(points: list[Point], ts: datetime, level: float, minutes: int) -> float | None:
    """Variação nos últimos `minutes` min, contra a leitura mais recente com essa idade (tolerância de 25%)."""
    target = ts - timedelta(minutes=minutes)
    tol = timedelta(minutes=max(2.0, 0.25 * minutes))
    candidates = [p for p in points if target - tol <= p.ts <= target]
    if not candidates:
        return None
    return level - max(candidates, key=lambda p: p.ts).level


def moving_avg(points: list[Point], ts: datetime, level: float, minutes: int = 60) -> float:
    values = [p.level for p in points if p.ts >= ts - timedelta(minutes=minutes)] + [level]
    return sum(values) / len(values)


def max_level(points: list[Point], ts: datetime, level: float, minutes: int = 180) -> float:
    return max([p.level for p in points if p.ts >= ts - timedelta(minutes=minutes)] + [level])


def rate_m_h(points: list[Point], ts: datetime, level: float, window_min: int = 60,
             min_points: int = 3, min_span_min: int = 15) -> float | None:
    """Taxa de subida em m/h: inclinação da reta ajustada às leituras dos últimos 60 min (robusta a ruído)."""
    pts = sorted([p for p in points if p.ts >= ts - timedelta(minutes=window_min)] + [Point(ts, level)],
                 key=lambda p: p.ts)
    if len(pts) < min_points:
        return None
    xs = [(p.ts - pts[0].ts).total_seconds() / 3600 for p in pts]
    if xs[-1] * 60 < min_span_min:
        return None
    ys = [p.level for p in pts]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - xm) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / den


def trend(rate: float | None, threshold: float) -> str | None:
    if rate is None:
        return None
    return "rising" if rate > threshold else "falling" if rate < -threshold else "steady"


def is_jump(baseline: Point | None, prev: Point | None, prev_suspect: bool,
            ts: datetime, level: float, rules: Rules) -> bool:
    """Variação impossível contra a última leitura confiável. Se a leitura anterior era suspeita e esta a
    confirma (fica perto dela), o nível é aceito: assim uma cheia real não é tratada como defeito para sempre."""
    if baseline is None:
        return False
    dt_h = max((ts - baseline.ts).total_seconds() / 3600, 1 / 60)
    if abs(level - baseline.level) / dt_h <= rules.max_rate_m_h:
        return False
    if prev is not None and prev_suspect and abs(level - prev.level) <= rules.confirm_tol_m:
        return False
    return True


def is_stuck(points: list[Point], ts: datetime, level: float, rules: Rules) -> bool:
    """Mesmo valor por muito tempo. Só sinaliza (aviso); pode ser um rio realmente estável."""
    pts = [p for p in points if p.ts >= ts - timedelta(minutes=rules.stuck_min)] + [Point(ts, level)]
    if len(pts) < rules.stuck_min_readings:
        return False
    span_min = (ts - min(p.ts for p in pts)).total_seconds() / 60
    if span_min < rules.stuck_min * 0.9:
        return False
    levels = [p.level for p in pts]
    return max(levels) - min(levels) <= rules.stuck_eps_m


def classify(level: float, rate: float | None, cotas: tuple[float | None, float | None, float | None],
             prev_risk: str, rules: Rules) -> tuple[str, list[str]]:
    """Regras determinísticas (independem de IA). cotas = (atenção, alerta, inundação), em metros."""
    attention, alert, flood = cotas
    if attention is None and alert is None and flood is None:
        return "unknown", ["cotas de risco não configuradas para esta estação"]

    risk, reasons = "normal", []
    if flood is not None and level >= flood:
        risk, reasons = "critical", [f"nível {m(level)} acima da cota de inundação ({m(flood)})"]
    elif alert is not None and level >= alert:
        risk, reasons = "alert", [f"nível {m(level)} acima da cota de alerta ({m(alert)})"]
    else:
        if attention is not None and level >= attention:
            risk, reasons = "attention", [f"nível {m(level)} acima da cota de atenção ({m(attention)})"]
        if rate is not None and rate >= rules.rise_attention_m_h:
            risk = "attention" if risk == "normal" else risk
            reasons.append(f"subindo {rate:.2f} m/h".replace(".", ","))

    # Histerese: o risco só desce quando o nível sai da cota com folga (evita alerta oscilando).
    if prev_risk in RISK_ORDER and prev_risk != "unknown" and RISK_ORDER[prev_risk] > RISK_ORDER[risk]:
        threshold = {"attention": attention, "alert": alert, "critical": flood}[prev_risk]
        level_clear = threshold is None or level < threshold - rules.hysteresis_m
        rate_clear = prev_risk != "attention" or rate is None or rate < rules.rise_attention_m_h * 0.5
        if not (level_clear and rate_clear):
            return prev_risk, [f"mantido em {RISK_PT[prev_risk]}: nível ainda próximo da cota ({m(level)})"]
    return risk, reasons or ["dentro dos limites"]
