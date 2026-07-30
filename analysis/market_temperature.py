# -*- coding: utf-8 -*-
"""
맥스웰-볼츠만(Maxwell-Boltzmann) 분포 아이디어를 거래량/변동성에 응용한 모듈

원본 자료의 핵심 개념:
  - 기체 분자 속도 분포처럼, 대부분의 거래일은 "평범한" 거래량을 보이고
    극히 일부(기관/외국인/큰손 개입일)만 "극단적인" 거래량(에너지)을 보인다.
  - "시장 온도"가 오르면 -> 변동성 증가, 거래량 증가, 공포/탐욕 증가.
  - 시장 온도의 급등이 큰 하락 전에 나타나는 경우가 있다는 연구가 존재.

이 모듈에서는 정식 맥스웰-볼츠만 분포를 엄밀히 피팅하기보다,
"평균 대비 얼마나 벗어난 에너지(거래) 상태인가"를 백분위/표준화 점수로
근사하여 실전에서 쓸 수 있는 형태로 구현했습니다. (Econophysics 참고모델)
"""

import statistics


def _percentile_rank(value, population):
    """population 내에서 value 가 차지하는 백분위(0~100)"""
    if not population:
        return 50.0
    below = sum(1 for v in population if v <= value)
    return below / len(population) * 100


def volume_energy_analysis(volumes, window: int = 20):
    """
    최근 거래량을 '거래 에너지'로 보고, 평균 대비 상태를 계산합니다.
    반환: dict(mean_volume, last_volume, ratio, percentile, state)
    """
    if len(volumes) < window + 1:
        return None

    hist = volumes[-(window + 1):-1]  # 오늘(마지막) 제외한 과거 window개
    last = volumes[-1]

    mean_v = statistics.mean(hist)
    std_v = statistics.pstdev(hist) or 1e-9
    ratio = last / mean_v if mean_v else 1.0
    pct = _percentile_rank(last, hist)
    z = (last - mean_v) / std_v

    if pct >= 90:
        state = "고에너지(거래 폭발) — 기관/외국인 등 큰손 개입 가능성"
    elif pct <= 20:
        state = "저에너지(거래 소강) — 관심 저조, 박스권 가능성"
    else:
        state = "평범한 에너지 상태 (대부분의 거래일과 유사)"

    return {
        "mean_volume": round(mean_v, 1),
        "last_volume": last,
        "ratio_vs_mean": round(ratio, 2),
        "percentile": round(pct, 1),
        "zscore": round(z, 3),
        "state": state,
    }


def market_temperature(closes, volumes, window: int = 20):
    """
    '시장 온도' = 최근 변동성(수익률 표준편차) + 거래량 에너지를 결합한 지표.
    0~100 스케일로 정규화 (100에 가까울수록 '뜨거운' 시장 = 고변동성/고거래량)
    """
    from .probability import daily_returns

    if len(closes) < window + 1 or len(volumes) < window + 1:
        return None

    rets = daily_returns(closes[-(window + 1):])
    recent_vol_std = statistics.pstdev(rets) if len(rets) > 1 else 0.0

    # 과거 여러 구간의 변동성 분포에서 현재 변동성의 백분위를 구함 (rolling)
    vol_series = []
    for i in range(window, len(closes) - 1):
        seg = daily_returns(closes[i - window:i + 1])
        if len(seg) > 1:
            vol_series.append(statistics.pstdev(seg))

    vol_percentile = _percentile_rank(recent_vol_std, vol_series) if vol_series else 50.0

    vol_energy = volume_energy_analysis(volumes, window=window)
    volume_percentile = vol_energy["percentile"] if vol_energy else 50.0

    temperature = round(0.5 * vol_percentile + 0.5 * volume_percentile, 1)

    if temperature >= 80:
        comment = "시장 온도 매우 높음 — 변동성/거래량 급등, 리스크 관리(현금 비중 확대) 권고"
    elif temperature >= 60:
        comment = "시장 온도 상승 중 — 변동성 확대 국면"
    elif temperature <= 30:
        comment = "시장 온도 낮음 — 박스권/관망 국면, 분할매수 전략에 유리할 수 있음"
    else:
        comment = "시장 온도 보통"

    return {
        "temperature": temperature,
        "volatility_percentile": round(vol_percentile, 1),
        "volume_percentile": round(volume_percentile, 1),
        "comment": comment,
    }


def market_temperature_score(closes, volumes, cfg) -> dict:
    """
    시장온도/거래에너지 종합 점수: -1.0 ~ +1.0
    - 저온 + 거래량 급증(바닥권 매집 정황) -> 매수 쪽
    - 고온 + 거래량 급증(과열 분출) -> 매도 경계 쪽
    다만 시장온도 자체는 방향성이 없으므로, 가격의 z-score(probability 모듈)와
    결합해서 signal_engine 에서 최종 방향을 정합니다. 여기서는 '리스크 신호'로서
    -1(고위험/변동성 급등 경계) ~ 0(중립) ~ +1(저변동성, 안정적 매집 구간) 로 표현합니다.
    """
    mtc = cfg["market_temperature"]
    temp_info = market_temperature(closes, volumes, window=mtc["volume_window"])
    vol_energy = volume_energy_analysis(volumes, window=mtc["volume_window"])

    if temp_info is None or vol_energy is None:
        return {"score": 0.0, "detail": {}, "note": "데이터 부족"}

    temperature = temp_info["temperature"]
    # 온도가 높을수록(과열/급변동) 리스크 점수는 낮아짐(음수 쪽)
    # 50을 중립으로 잡고 선형 매핑
    score = (50 - temperature) / 50  # temp=0 -> +1.0, temp=100 -> -1.0
    score = max(-1.0, min(1.0, score))

    return {
        "score": round(score, 3),
        "detail": {"market_temperature": temp_info, "volume_energy": vol_energy},
    }
