# -*- coding: utf-8 -*-
"""추세 분석: 이동평균 정배열/역배열, RSI, MACD"""

import statistics


def sma(values, period):
    if len(values) < period:
        return None
    return statistics.mean(values[-period:])


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [statistics.mean(values[:period])]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    diff_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-diff_len:][i] - ema_slow[-diff_len:][i] for i in range(diff_len)]
    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return None
    hist = macd_line[-1] - signal_line[-1]
    return {
        "macd": round(macd_line[-1], 2),
        "signal": round(signal_line[-1], 2),
        "histogram": round(hist, 2),
    }


def trend_analysis(closes, cfg):
    t = cfg["trend"]
    ma_s = sma(closes, t["ma_short"])
    ma_m = sma(closes, t["ma_mid"])
    ma_l = sma(closes, t["ma_long"])
    r = rsi(closes, t["rsi_period"])
    m = macd(closes, t["macd_fast"], t["macd_slow"], t["macd_signal"])

    if None in (ma_s, ma_m, ma_l, r):
        return None

    if ma_s > ma_m > ma_l:
        alignment = "정배열 (단기>중기>장기) — 상승 추세"
    elif ma_s < ma_m < ma_l:
        alignment = "역배열 (단기<중기<장기) — 하락 추세"
    else:
        alignment = "혼조 (추세 전환 구간 가능성)"

    return {
        "ma_short": round(ma_s, 1),
        "ma_mid": round(ma_m, 1),
        "ma_long": round(ma_l, 1),
        "alignment": alignment,
        "rsi": round(r, 1),
        "macd": m,
    }


def trend_score(closes, cfg) -> dict:
    """추세 종합 점수: -1.0 ~ +1.0"""
    info = trend_analysis(closes, cfg)
    if info is None:
        return {"score": 0.0, "detail": {}, "note": "데이터 부족"}

    t = cfg["trend"]
    score = 0.0

    # 이동평균 배열 (가중치 0.5)
    if info["ma_short"] > info["ma_mid"] > info["ma_long"]:
        score += 0.5
    elif info["ma_short"] < info["ma_mid"] < info["ma_long"]:
        score -= 0.5

    # RSI (가중치 0.3) : 과매도 -> 매수쪽, 과매수 -> 매도쪽
    rsi_v = info["rsi"]
    if rsi_v <= t["rsi_oversold"]:
        score += 0.3
    elif rsi_v >= t["rsi_overbought"]:
        score -= 0.3
    else:
        # 30~70 구간은 50을 기준으로 선형 보간 (약한 신호)
        score += 0.3 * ((50 - rsi_v) / 20) * 0.3

    # MACD 히스토그램 (가중치 0.2)
    if info["macd"]:
        hist = info["macd"]["histogram"]
        if hist > 0:
            score += 0.2
        elif hist < 0:
            score -= 0.2

    score = max(-1.0, min(1.0, score))
    return {"score": round(score, 3), "detail": info}
