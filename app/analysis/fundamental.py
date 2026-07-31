# -*- coding: utf-8 -*-
"""
펀더멘털 분석 (단순 절대기준 스코어링)

주의: 업종 평균/시장 평균과의 정밀 비교가 아닌, 일반적으로 통용되는
절대적 임계값(config.json 에서 조정 가능) 기준의 단순 참고 점수입니다.
실제 투자 판단 시에는 업종 특성, 성장성, 부채비율 등 더 많은 지표를
함께 고려하시길 권장합니다.
"""


def fundamental_score(fundamental_data: dict, cfg) -> dict:
    f = cfg["fundamental"]
    per = fundamental_data.get("PER", 0)
    pbr = fundamental_data.get("PBR", 0)
    roe = fundamental_data.get("ROE", 0)

    score = 0.0
    notes = []

    # PER (가중치 0.4)
    if per <= 0:
        notes.append("PER 데이터 없음/적자 — 중립 처리")
    elif per <= f["per_good_below"]:
        score += 0.4
        notes.append(f"PER {per} — 저평가 구간")
    elif per >= f["per_bad_above"]:
        score -= 0.4
        notes.append(f"PER {per} — 고평가 구간")
    else:
        notes.append(f"PER {per} — 보통 수준")

    # PBR (가중치 0.3)
    if pbr <= 0:
        notes.append("PBR 데이터 없음 — 중립 처리")
    elif pbr <= f["pbr_good_below"]:
        score += 0.3
        notes.append(f"PBR {pbr} — 자산가치 대비 저평가")
    elif pbr >= f["pbr_bad_above"]:
        score -= 0.3
        notes.append(f"PBR {pbr} — 자산가치 대비 고평가")
    else:
        notes.append(f"PBR {pbr} — 보통 수준")

    # ROE (가중치 0.3)
    if roe >= f["roe_good_above"]:
        score += 0.3
        notes.append(f"ROE {roe}% — 수익성 양호")
    elif roe == 0:
        # PER/PBR과 동일하게, 정확히 0은 '데이터 없음'(가상자산 등 재무지표가 없는
        # 경우 포함)으로 간주해 중립 처리합니다. 진짜 적자(음수)만 감점합니다.
        notes.append("ROE 데이터 없음 — 중립 처리")
    elif roe < 0:
        score -= 0.3
        notes.append(f"ROE {roe}% — 수익성 부진/적자")
    else:
        notes.append(f"ROE {roe}% — 보통 수준")

    score = max(-1.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "detail": {"PER": per, "PBR": pbr, "ROE": roe, "notes": notes},
    }
