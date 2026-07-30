# -*- coding: utf-8 -*-
"""
종합 신호 엔진

probability(정규분포) + market_temperature(맥스웰-볼츠만 응용) + trend(추세)
+ fundamental(펀더멘털) + risk(리스크 관리) 점수를 config.json 의 가중치로
합산해 최종 BUY / SELL / HOLD 신호와 근거를 생성합니다.
"""

from . import probability, market_temperature, trend, fundamental, risk


def analyze_stock(code: str, name: str, ohlc_rows: list, fundamental_data: dict, cfg) -> dict:
    """
    ohlc_rows: [{"date","open","high","low","close","volume"}, ...] 과거->최근 순
    fundamental_data: kiwoom_api.get_fundamental() 결과
    """
    if len(ohlc_rows) < 30:
        return {
            "code": code, "name": name,
            "signal": "HOLD", "final_score": 0.0,
            "note": "분석에 필요한 데이터가 부족합니다 (최소 30영업일 이상 필요).",
        }

    closes = [row["close"] for row in ohlc_rows]
    volumes = [row["volume"] for row in ohlc_rows]

    prob_result = probability.probability_score(closes, cfg)
    temp_result = market_temperature.market_temperature_score(closes, volumes, cfg)
    trend_result = trend.trend_score(closes, cfg)
    fund_result = fundamental.fundamental_score(fundamental_data, cfg)
    risk_result = risk.risk_plan(ohlc_rows, cfg)

    w = cfg["signal"]["weights"]
    final_score = (
        prob_result["score"] * w["probability"]
        + temp_result["score"] * w["market_temperature"]
        + trend_result["score"] * w["trend"]
        + fund_result["score"] * w["fundamental"]
        + risk_result["score"] * w["risk"]
    )
    final_score = round(final_score, 3)

    buy_th = cfg["signal"]["buy_threshold"]
    sell_th = cfg["signal"]["sell_threshold"]

    if final_score >= buy_th:
        signal = "BUY"
    elif final_score <= sell_th:
        signal = "SELL"
    else:
        signal = "HOLD"

    reasons = _build_reasons(prob_result, temp_result, trend_result, fund_result, risk_result)

    return {
        "code": code,
        "name": name,
        "signal": signal,
        "final_score": final_score,
        "current_price": closes[-1],
        "components": {
            "probability": prob_result,
            "market_temperature": temp_result,
            "trend": trend_result,
            "fundamental": fund_result,
            "risk": risk_result,
        },
        "reasons": reasons,
    }


def _build_reasons(prob_r, temp_r, trend_r, fund_r, risk_r) -> list:
    reasons = []

    zinfo = prob_r.get("detail", {}).get("zscore")
    if zinfo:
        reasons.append(f"[확률분포] {zinfo['interpretation']} (z={zinfo['z']})")

    bb = prob_r.get("detail", {}).get("bollinger")
    if bb:
        pos_map = {"above": "상단 돌파(과열)", "below": "하단 이탈(과매도)", "inside": "밴드 내부"}
        reasons.append(f"[볼린저밴드] {pos_map.get(bb['position'])} (현재가 {bb['price']} / 밴드 {bb['lower']}~{bb['upper']})")

    mt = temp_r.get("detail", {}).get("market_temperature")
    if mt:
        reasons.append(f"[시장온도] {mt['temperature']}/100 — {mt['comment']}")

    ve = temp_r.get("detail", {}).get("volume_energy")
    if ve:
        reasons.append(f"[거래에너지] {ve['state']} (평균 대비 {ve['ratio_vs_mean']}배, 상위 {round(100-ve['percentile'],1)}%)")

    tinfo = trend_r.get("detail")
    if tinfo:
        reasons.append(f"[추세] {tinfo['alignment']}, RSI {tinfo['rsi']}")

    fnotes = fund_r.get("detail", {}).get("notes")
    if fnotes:
        reasons.append("[펀더멘털] " + ", ".join(fnotes))

    rdetail = risk_r.get("detail")
    if rdetail:
        reasons.append(
            f"[리스크관리] 손절 {rdetail.get('stop_loss')} / 목표 {rdetail.get('target_price')} "
            f"(손익비 {rdetail.get('risk_reward_ratio')}), 추천수량 {rdetail.get('suggested_qty')}주"
        )

    return reasons
