# -*- coding: utf-8 -*-
"""
업비트(Upbit) 공개 API 연동 — 가상자산(코인) 시세/검색

시세 조회용 공개 엔드포인트는 별도 인증키가 필요 없습니다.
공식 문서: https://docs.upbit.com/

naver.py와 동일한 함수 시그니처(get_daily_ohlcv, get_fundamental, search_stocks)를
맞춰서, scheduler/main에서 증권(stock)과 가상자산(crypto)을 같은 방식으로 다룰 수
있도록 했습니다.
"""

import time

import requests

API_BASE = "https://api.upbit.com/v1"
HEADERS = {"Accept": "application/json"}

_market_cache = {"data": None, "fetched_at": 0}
_MARKET_CACHE_TTL_SEC = 300  # 전체 마켓 목록은 자주 안 바뀌므로 5분 캐시


def get_daily_ohlcv(code: str, lookback_days: int = 120):
    """
    일별 캔들(candles/days)을 조회합니다. code는 업비트 마켓 코드(예: KRW-BTC).
    반환: [{"date","open","high","low","close","volume"}, ...] 과거->최근 순
    """
    count = min(max(lookback_days, 1), 200)  # 업비트는 요청당 최대 200개까지 지원
    try:
        resp = requests.get(
            f"{API_BASE}/candles/days", params={"market": code, "count": count},
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[업비트 시세 오류] {code}: {e}")
        return []

    rows = [
        {
            "date": item["candle_date_time_kst"][:10].replace("-", ""),
            "open": item["opening_price"],
            "high": item["high_price"],
            "low": item["low_price"],
            "close": item["trade_price"],
            "volume": item["candle_acc_trade_volume"],
        }
        for item in data
    ]
    rows.reverse()  # 업비트는 최신->과거 순으로 내려주므로 과거->최근 순으로 맞춤
    return rows


def get_fundamental(code: str):
    """
    코인은 PER/PBR/ROE 같은 전통적 재무 지표가 없어 항상 0(중립)으로 반환합니다.
    signal_engine의 펀더멘털 모듈은 이 경우 '중립 처리'하도록 이미 구현되어 있습니다.
    """
    return {"종목명": code, "현재가": 0, "PER": 0, "PBR": 0, "ROE": 0, "EPS": 0, "시가총액": 0}


def _get_all_markets_cached():
    now = time.time()
    if _market_cache["data"] is not None and now - _market_cache["fetched_at"] < _MARKET_CACHE_TTL_SEC:
        return _market_cache["data"]

    try:
        resp = requests.get(f"{API_BASE}/market/all", params={"isDetails": "false"}, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[업비트 마켓 목록 오류] {e}")
        return _market_cache["data"] or []

    markets = [
        {"code": m["market"], "name": m["korean_name"]}
        for m in data if m["market"].startswith("KRW-")
    ]
    _market_cache["data"] = markets
    _market_cache["fetched_at"] = now
    return markets


def search_stocks(query: str, limit: int = 15):
    """코인명(한글/영문) 또는 마켓코드로 검색해 {code, name} 목록을 반환합니다."""
    query = (query or "").strip()
    if not query:
        return []
    q_lower = query.lower()
    markets = _get_all_markets_cached()
    results = [m for m in markets if q_lower in m["name"].lower() or q_lower in m["code"].lower()]
    return results[:limit]
