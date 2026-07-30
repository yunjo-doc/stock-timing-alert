# -*- coding: utf-8 -*-
"""
대시보드 렌더링 보조 유틸

- 정규분포 종모양(bell curve) SVG 경로 생성 (z-score 마커 포함)
- 맥스웰-볼츠만 형태의 비대칭 분포 SVG 경로 생성 (시장온도 마커 포함)
- 관심종목 전체를 요약하는 '시장 요약 카드' 통계
- 가장 신호가 강한 종목을 골라 '타이밍 분석 / 시그널 타임라인 / AI 분석' 패널에 사용
"""

import math


def _normal_pdf(x):
    return math.exp(-(x ** 2) / 2) / math.sqrt(2 * math.pi)


def bell_curve_path(z_score, width=520, height=160, pad=10):
    """표준정규분포 종모양 곡선의 SVG polyline 좌표 문자열과, z-score 마커의 (x,y) 픽셀 좌표를 반환"""
    x_min, x_max = -3.6, 3.6
    max_y = _normal_pdf(0)
    points = []
    n = 80
    for i in range(n + 1):
        x = x_min + (x_max - x_min) * i / n
        y = _normal_pdf(x)
        px = pad + (x - x_min) / (x_max - x_min) * (width - 2 * pad)
        py = height - pad - (y / max_y) * (height - 2 * pad)
        points.append(f"{px:.1f},{py:.1f}")

    z_clamped = max(x_min, min(x_max, z_score if z_score is not None else 0))
    marker_x = pad + (z_clamped - x_min) / (x_max - x_min) * (width - 2 * pad)
    marker_y = height - pad - (_normal_pdf(z_clamped) / max_y) * (height - 2 * pad)

    # 영역 채우기용 닫힌 path (곡선 아래 면적)
    area_points = points + [f"{width - pad:.1f},{height - pad:.1f}", f"{pad:.1f},{height - pad:.1f}"]

    return {
        "line_points": " ".join(points),
        "area_points": " ".join(area_points),
        "marker_x": round(marker_x, 1),
        "marker_y": round(marker_y, 1),
        "width": width,
        "height": height,
    }


def _mb_pdf(x, a=1.4):
    # 맥스웰-볼츠만 속도분포와 같은 형태: x^2 * exp(-x^2 / (2a^2))  (x>=0)
    return (x ** 2) * math.exp(-(x ** 2) / (2 * a ** 2))


def maxwell_boltzmann_path(percentile, width=520, height=160, pad=10):
    """
    거래/변동성 에너지를 맥스웰-볼츠만 속도분포 형태의 비대칭 곡선으로 표현.
    percentile(0~100)을 x축 위치로 매핑해 마커를 찍는다 (100에 가까울수록 '고에너지' 오른쪽 꼬리 쪽).
    """
    x_min, x_max = 0.0, 5.0
    xs = [x_min + (x_max - x_min) * i / 200 for i in range(201)]
    ys = [_mb_pdf(x) for x in xs]
    max_y = max(ys)

    points = []
    n = 80
    for i in range(n + 1):
        x = x_min + (x_max - x_min) * i / n
        y = _mb_pdf(x)
        px = pad + (x - x_min) / (x_max - x_min) * (width - 2 * pad)
        py = height - pad - (y / max_y) * (height - 2 * pad)
        points.append(f"{px:.1f},{py:.1f}")

    pct = 50 if percentile is None else percentile
    marker_x_val = x_min + (x_max - x_min) * (pct / 100)
    marker_x = pad + (marker_x_val - x_min) / (x_max - x_min) * (width - 2 * pad)
    marker_y = height - pad - (_mb_pdf(marker_x_val) / max_y) * (height - 2 * pad)

    area_points = points + [f"{width - pad:.1f},{height - pad:.1f}", f"{pad:.1f},{height - pad:.1f}"]

    return {
        "line_points": " ".join(points),
        "area_points": " ".join(area_points),
        "marker_x": round(marker_x, 1),
        "marker_y": round(marker_y, 1),
        "width": width,
        "height": height,
    }


def market_summary(signals: list):
    """관심종목 전체를 요약하는 4개 카드용 통계"""
    if not signals:
        return {
            "overall_score_10": 0, "avg_temperature": 0, "risk_level": "—",
            "buy_count": 0, "sell_count": 0, "hold_count": 0, "dominant_signal": "HOLD",
        }

    scores = [s["final_score"] for s in signals if s.get("final_score") is not None]
    temps = [s["market_temperature"] for s in signals if s.get("market_temperature") is not None]
    risk_scores = [s["risk_score"] for s in signals if s.get("risk_score") is not None]

    avg_score = sum(scores) / len(scores) if scores else 0
    overall_score_10 = round((avg_score + 1) / 2 * 10, 1)  # -1~1 -> 0~10
    avg_temperature = round(sum(temps) / len(temps), 1) if temps else 50

    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 0
    if avg_temperature >= 70 or avg_risk < -0.2:
        risk_level = "High"
    elif avg_temperature <= 35 and avg_risk >= 0.3:
        risk_level = "Low"
    else:
        risk_level = "Medium"

    buy_count = sum(1 for s in signals if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals if s["signal"] == "SELL")
    hold_count = sum(1 for s in signals if s["signal"] == "HOLD")
    dominant_signal = max(("BUY", "SELL", "HOLD"), key=lambda k: {"BUY": buy_count, "SELL": sell_count, "HOLD": hold_count}[k])

    return {
        "overall_score_10": overall_score_10,
        "avg_temperature": avg_temperature,
        "risk_level": risk_level,
        "buy_count": buy_count, "sell_count": sell_count, "hold_count": hold_count,
        "dominant_signal": dominant_signal,
    }


def pick_featured_stock(signals: list):
    """가장 신호가 강한(절대값 기준) 종목 1개를 골라 상세 분석 패널에 사용"""
    if not signals:
        return None
    return max(signals, key=lambda s: abs(s.get("final_score") or 0))


def build_signal_timeline(stock: dict):
    """featured 종목 하나에 대한 5단계 시그널 타임라인 상태"""
    if not stock:
        return []

    steps = [
        {"label": "시가 형성", "detail": f"{stock.get('current_price') or 0:,.0f}원", "state": "done"},
        {
            "label": "확률 신호 감지",
            "detail": f"z={stock.get('z_score')}" if stock.get("z_score") is not None else "데이터 없음",
            "state": "done" if stock.get("z_score") is not None else "pending",
        },
        {
            "label": "거래 에너지 확인",
            "detail": f"평균 대비 {stock.get('volume_ratio')}배" if stock.get("volume_ratio") is not None else "데이터 없음",
            "state": "done" if stock.get("volume_ratio") is not None else "pending",
        },
        {
            "label": "추세 확인",
            "detail": stock.get("trend_alignment") or "데이터 없음",
            "state": "done" if stock.get("trend_alignment") else "pending",
        },
        {
            "label": f"최종 판단: {stock.get('signal')}",
            "detail": f"종합점수 {stock.get('final_score')}",
            "state": "current",
        },
    ]
    return steps
