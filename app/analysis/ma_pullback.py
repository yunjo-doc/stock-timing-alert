# -*- coding: utf-8 -*-
"""
이동평균선(10/20/50일선) 정배열·역배열 + 눌림목/되돌림 전략

유튜브에서 소개된 매매법을 그대로 규칙화한 것입니다:
- 롱(매수): 정배열(10일선>20일선>50일선) 상태에서 종가가 10일선 아래로 눌렸다가
  (단, 50일선은 이탈하지 않아야 함) 다시 10일선 위로 돌파 마감하면 진입.
  1차 익절은 종가가 다시 10일선 아래로, 2차 익절은 20일선 아래로 내려올 때.
  손절은 종가가 50일선 아래로 이탈할 때.
- 숏(매도): 역배열(10일선<20일선<50일선) 상태에서 종가가 10일선 위로 반등했다가
  (단, 50일선은 이탈하지 않아야 함) 다시 10일선 아래로 이탈 마감하면 진입. 나머지는 대칭.

기존 5요소 신호 엔진(signal_engine.py)과는 완전히 별개의 독립적인 전략이며,
BUY/SELL/HOLD 점수 대신 정배열/역배열 구간에서의 눌림목 진행 상태를 그대로 보여줍니다.
"""


def _sma_series(values: list, period: int) -> list:
    """단순이동평균 시계열. 앞의 (period-1)개는 None."""
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1: i + 1]) / period
    return out


def ma_pullback_signal(closes: list, cfg: dict) -> dict:
    """
    반환 setup 값:
      LONG_ENTRY   - 오늘 매수 진입 신호 (눌림목 후 10일선 돌파 마감)
      LONG_WAITING - 정배열 상태에서 눌림목 진행 중 (10일선 돌파 대기)
      LONG_INVALID - 눌림목 중 50일선 이탈로 세팅 무효화
      SHORT_ENTRY / SHORT_WAITING / SHORT_INVALID - 위와 대칭(역배열/반등)
      NONE         - 정배열/역배열이 아니거나 데이터 부족
    """
    p = cfg.get("ma_pullback", {"short": 10, "mid": 20, "long": 50})
    short_p, mid_p, long_p = p["short"], p["mid"], p["long"]

    n = len(closes)
    if n < long_p + 1:
        return {"setup": "NONE", "detail": "데이터 부족", "ma_short": None, "ma_mid": None, "ma_long": None}

    ma_short = _sma_series(closes, short_p)
    ma_mid = _sma_series(closes, mid_p)
    ma_long = _sma_series(closes, long_p)
    today = n - 1

    def bullish(i):
        return ma_short[i] is not None and ma_mid[i] is not None and ma_long[i] is not None \
            and ma_short[i] > ma_mid[i] > ma_long[i]

    def bearish(i):
        return ma_short[i] is not None and ma_mid[i] is not None and ma_long[i] is not None \
            and ma_short[i] < ma_mid[i] < ma_long[i]

    levels = {
        "ma_short": round(ma_short[today], 2),
        "ma_mid": round(ma_mid[today], 2),
        "ma_long": round(ma_long[today], 2),
    }

    if bullish(today):
        # 오늘 막 눌림목을 뚫고 10일선 위로 돌파 마감했는지 확인
        if closes[today] > ma_short[today] and closes[today - 1] <= ma_short[today - 1]:
            j, found_pullback, invalidated = today - 1, False, False
            while j >= 0 and bullish(j) and closes[j] < ma_short[j]:
                found_pullback = True
                if closes[j] < ma_long[j]:
                    invalidated = True
                    break
                j -= 1
            if found_pullback and not invalidated:
                return {"setup": "LONG_ENTRY",
                        "detail": "정배열 눌림목 후 10일선 돌파 마감 — 매수 진입 시점", **levels}
        if closes[today] < ma_short[today]:
            if closes[today] < ma_long[today]:
                return {"setup": "LONG_INVALID", "detail": "눌림목 중 50일선 이탈 — 세팅 무효화", **levels}
            return {"setup": "LONG_WAITING", "detail": "정배열 눌림목 진행 중 — 10일선 돌파 대기", **levels}
        return {"setup": "NONE", "detail": "정배열 상승 추세 지속 중 (눌림목 없음)", **levels}

    if bearish(today):
        if closes[today] < ma_short[today] and closes[today - 1] >= ma_short[today - 1]:
            j, found_bounce, invalidated = today - 1, False, False
            while j >= 0 and bearish(j) and closes[j] > ma_short[j]:
                found_bounce = True
                if closes[j] > ma_long[j]:
                    invalidated = True
                    break
                j -= 1
            if found_bounce and not invalidated:
                return {"setup": "SHORT_ENTRY",
                        "detail": "역배열 반등 후 10일선 이탈 마감 — 매도 진입 시점", **levels}
        if closes[today] > ma_short[today]:
            if closes[today] > ma_long[today]:
                return {"setup": "SHORT_INVALID", "detail": "반등 중 50일선 돌파 — 세팅 무효화", **levels}
            return {"setup": "SHORT_WAITING", "detail": "역배열 반등 진행 중 — 10일선 이탈 대기", **levels}
        return {"setup": "NONE", "detail": "역배열 하락 추세 지속 중 (반등 없음)", **levels}

    return {"setup": "NONE", "detail": "정배열/역배열 아님 (이평선 혼조)", **levels}
