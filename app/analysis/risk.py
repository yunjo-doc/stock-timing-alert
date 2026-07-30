# -*- coding: utf-8 -*-
"""
리스크 관리 모듈

원본 자료의 "손절라인을 2σ 정도로 설정" 개념을 확장하여,
실전에서 더 널리 쓰이는 ATR(Average True Range) 기반 손절/목표가와
계좌 자본 대비 포지션 사이징(1~2% 룰)을 함께 계산합니다.
"""


def atr(ohlc_rows, period=14):
    """
    ohlc_rows: [{"high":..,"low":..,"close":..}, ...] (과거->최근 순)
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    if len(ohlc_rows) < period + 1:
        return None

    trs = []
    for i in range(1, len(ohlc_rows)):
        h = ohlc_rows[i]["high"]
        l = ohlc_rows[i]["low"]
        pc = ohlc_rows[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    recent_trs = trs[-period:]
    return sum(recent_trs) / len(recent_trs)


def risk_plan(ohlc_rows, cfg) -> dict:
    """
    ATR 기반 손절가/목표가, 그리고 계좌 자본 대비 추천 매수 수량을 계산합니다.
    """
    r = cfg["risk_management"]
    a = atr(ohlc_rows, period=r["atr_period"])
    if a is None:
        return {"score": 0.0, "detail": {}, "note": "ATR 계산을 위한 데이터 부족"}

    last_close = ohlc_rows[-1]["close"]
    stop_loss = round(last_close - a * r["atr_stop_multiple"])
    target = round(last_close + a * r["atr_target_multiple"])

    risk_amount = r["account_capital_krw"] * (r["risk_per_trade_pct"] / 100)
    per_share_risk = max(last_close - stop_loss, 1)
    suggested_qty = int(risk_amount // per_share_risk)

    # 종목당 최대 비중 제한 적용
    max_position_value = r["account_capital_krw"] * (r["max_position_pct_per_stock"] / 100)
    max_qty_by_position = int(max_position_value // last_close) if last_close else 0
    suggested_qty = min(suggested_qty, max_qty_by_position)

    risk_reward_ratio = round((target - last_close) / max(last_close - stop_loss, 1), 2)

    # 리스크 점수: 손절폭이 좁고(ATR 대비 안정적), 손익비가 좋을수록 +
    # 단순화: risk_reward_ratio 가 1.5 이상이면 우호적(+), 1.0 미만이면 비우호(-)
    if risk_reward_ratio >= 2.0:
        score = 1.0
    elif risk_reward_ratio >= 1.5:
        score = 0.5
    elif risk_reward_ratio >= 1.0:
        score = 0.0
    else:
        score = -0.5

    return {
        "score": round(score, 3),
        "detail": {
            "atr": round(a, 1),
            "current_price": last_close,
            "stop_loss": stop_loss,
            "target_price": target,
            "risk_reward_ratio": risk_reward_ratio,
            "suggested_qty": max(suggested_qty, 0),
            "position_value_krw": max(suggested_qty, 0) * last_close,
        },
    }
