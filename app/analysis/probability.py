# -*- coding: utf-8 -*-
"""
정규분포(Normal Distribution) 기반 확률 분석 모듈

원본 자료의 핵심 개념을 그대로 구현합니다.
  - 일간 수익률은 평균/표준편차를 갖는 정규분포에 가깝다고 "가정"
  - ±1σ ≈ 68%, ±2σ ≈ 95%, ±3σ ≈ 99.7%
  - 볼린저 밴드 = 20일 이동평균 ± 2σ
  - +3σ 근접 = 과열(조정 가능성), -3σ 근접 = 과매도(반등 가능성)

주의: 실제 주가 수익률은 정규분포보다 꼬리가 두꺼운(fat-tail) 경우가 많습니다.
      이 모듈의 z-score/확률은 "참고 지표"이지 절대적인 확률이 아닙니다.
"""

import math
import statistics


def _erf(x):
    # 표준정규분포 CDF 계산용 (외부 라이브러리 없이 근사)
    return math.erf(x)


def normal_cdf(z: float) -> float:
    """표준정규분포 누적분포함수 P(Z <= z)"""
    return 0.5 * (1 + _erf(z / math.sqrt(2)))


def daily_returns(closes):
    """종가 리스트 -> 일간 수익률(%) 리스트"""
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] == 0:
            continue
        r = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        returns.append(r)
    return returns


def zscore_analysis(closes, window: int = 20):
    """
    최근 window 기간의 평균/표준편차 대비, 가장 최근 종가의 z-score 계산.
    반환: dict(mean, std, last_return_pct, z, probability_beyond, interpretation)
    """
    rets = daily_returns(closes[-(window + 1):])
    if len(rets) < 5:
        return None

    mean = statistics.mean(rets)
    std = statistics.pstdev(rets) or 1e-9
    last_return = rets[-1]
    z = (last_return - mean) / std

    # 이 수익률(혹은 그 이상)이 나올 "편측" 확률 (극단성 척도)
    prob_beyond = 1 - normal_cdf(abs(z))

    if z >= 3:
        interp = "＋3σ 이상: 과열 구간, 단기 조정 가능성 높음"
    elif z >= 2:
        interp = "＋2σ 이상: 과열 신호, 상단 돌파(볼린저 밴드 상단) 경계"
    elif z <= -3:
        interp = "－3σ 이하: 과매도 구간, 반등 가능성 높음"
    elif z <= -2:
        interp = "－2σ 이하: 과매도 신호, 하단 이탈(볼린저 밴드 하단) 근접"
    else:
        interp = "평균 범위 내 (±2σ 이내), 통계적으로 흔한 움직임"

    return {
        "mean_return_pct": round(mean, 3),
        "std_return_pct": round(std, 3),
        "last_return_pct": round(last_return, 3),
        "z": round(z, 3),
        "probability_beyond_pct": round(prob_beyond * 100, 2),
        "interpretation": interp,
    }


def _round_price(value, digits: int = 1):
    """1원 미만(초소액 가상자산 등)은 소수점 1자리 반올림 시 0으로 뭉개지므로,
    유효숫자가 보이도록 자리수를 늘려서 반올림합니다."""
    return round(value, digits) if abs(value) >= 1 else round(value, 8)


def bollinger_bands(closes, window: int = 20, k: float = 2.0):
    """
    볼린저 밴드 = window 이동평균 ± k * 표준편차(가격 기준, %가 아닌 원 단위)
    반환: dict(ma, upper, lower, price, position) position: 'above'/'below'/'inside'
    """
    if len(closes) < window:
        return None

    recent = closes[-window:]
    ma = statistics.mean(recent)
    std = statistics.pstdev(recent) or 1e-9
    upper = ma + k * std
    lower = ma - k * std
    price = closes[-1]

    if price > upper:
        position = "above"  # 상단 돌파 -> 과열
    elif price < lower:
        position = "below"  # 하단 이탈 -> 과매도
    else:
        position = "inside"

    return {
        "ma": _round_price(ma),
        "upper": _round_price(upper),
        "lower": _round_price(lower),
        "price": price,
        "position": position,
    }


def probability_score(closes, cfg) -> dict:
    """
    확률(정규분포) 종합 점수: -1.0(강한 매도) ~ +1.0(강한 매수)
    - z-score 가 매우 낮음(-3σ 근처) -> 반등 기대 -> 매수 쪽 점수(+)
    - z-score 가 매우 높음(+3σ 근처) -> 과열 -> 매도 쪽 점수(-)
    """
    p = cfg["probability"]
    z_info = zscore_analysis(closes, window=p["bollinger_window"])
    bb_info = bollinger_bands(closes, window=p["bollinger_window"], k=p["bollinger_k"])

    if z_info is None or bb_info is None:
        return {"score": 0.0, "detail": {"z": None, "bollinger": None}, "note": "데이터 부족"}

    z = z_info["z"]
    extreme = p["extreme_z"]

    # z를 [-extreme, +extreme] 범위로 clip 후 부호를 반대로(과매도->매수, 과열->매도)
    z_clipped = max(-extreme, min(extreme, z))
    score = -z_clipped / extreme  # z=-3 -> score=+1.0(매수), z=+3 -> score=-1.0(매도)

    return {
        "score": round(score, 3),
        "detail": {"zscore": z_info, "bollinger": bb_info},
    }
