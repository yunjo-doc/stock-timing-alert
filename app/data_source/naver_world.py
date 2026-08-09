# -*- coding: utf-8 -*-
"""
네이버 해외증시(api.stock.naver.com) 비공식 연동 모듈

⚠️ 주의사항
- 국내(naver.py)와 마찬가지로 네이버가 공식 제공하는 API가 아니라, 네이버 증권
  해외증시 페이지가 내부적으로 호출하는 JSON API를 그대로 사용하는 "비공식" 방식입니다.
- 네이버가 응답 구조를 변경하면 파싱이 깨질 수 있습니다.
- 종목 코드는 로이터 코드(예: 'AAPL.O', 'TSLA.O')를 사용합니다. 국내 6자리 코드와
  형식이 달라 watchlist에는 이 로이터 코드를 그대로 저장합니다.
- 상업적 서비스로 확장 시에는 공식 유료 시세 데이터 제공업체 사용을 권장합니다.
"""

import re
import time

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}

SEARCH_URL = "https://ac.stock.naver.com/ac"
CHART_URL = "https://api.stock.naver.com/chart/foreign/item/{code}"
BASIC_URL = "https://api.stock.naver.com/stock/{code}/basic"
INDEX_URL = "https://api.stock.naver.com/index/{code}/basic"

_index_cache = {"data": None, "fetched_at": 0}
_INDEX_CACHE_TTL_SEC = 300  # 대시보드 히어로 카드용 지수는 5분 캐시로 충분


def search_stocks(query: str, limit: int = 15):
    """종목명(한글/영문) 또는 티커로 해외 종목을 검색해 {code, name} 목록을 반환합니다.
    code는 로이터 코드(예: 'AAPL.O')입니다."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        resp = requests.get(SEARCH_URL, params={"q": query, "target": "worldstock"},
                             headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[네이버 해외증시 검색 오류] query={query}: {e}")
        return []

    results = []
    for item in data.get("items", []):
        code = item.get("reutersCode")
        name = item.get("name")
        if not code or not name:
            continue
        results.append({"code": code, "name": name})
        if len(results) >= limit:
            break
    return results


def get_daily_ohlcv(code: str, lookback_days: int = 120):
    """
    일별 캔들(거래량 포함)을 조회합니다. 네이버가 종목당 최근 약 110영업일치를
    제공하는 걸로 확인되어, lookback_days가 이보다 크더라도 있는 만큼만 반환합니다.
    반환: [{"date","open","high","low","close","volume"}, ...] 과거->최근 순
    """
    try:
        resp = requests.get(
            CHART_URL.format(code=code),
            params={"periodType": "dayCandle", "startDateTime": "", "count": lookback_days},
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[네이버 해외증시 시세 오류] {code}: {e}")
        return []

    rows = []
    for item in data.get("priceInfos", []):
        try:
            rows.append({
                "date": item["localDate"],
                "open": float(item["openPrice"]),
                "high": float(item["highPrice"]),
                "low": float(item["lowPrice"]),
                "close": float(item["closePrice"]),
                "volume": int(item.get("accumulatedTradingVolume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _parse_ratio(value: str) -> float:
    """'36.05배' 같은 형식에서 숫자만 뽑아냅니다. 파싱 실패/N-A는 0(데이터 없음)으로 처리."""
    if not value:
        return 0.0
    m = re.search(r"-?[\d,]+\.?\d*", value)
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0


def get_fundamental(code: str):
    """
    종목 기본정보(basic) 페이지에서 PER, PBR, 현재가 등을 추출합니다.
    ROE는 이 API에서 별도로 제공되지 않아 0(데이터 없음 -> 중립 처리)으로 둡니다.
    실패 시 0으로 채워 signal_engine이 '중립' 처리하도록 합니다.
    """
    try:
        resp = requests.get(BASIC_URL.format(code=code), headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[네이버 해외증시 기본정보 오류] {code}: {e}")
        return {"종목명": code, "현재가": 0, "PER": 0, "PBR": 0, "ROE": 0, "EPS": 0, "시가총액": 0}

    name = data.get("stockName") or code
    try:
        price = float(str(data.get("closePrice", "0")).replace(",", ""))
    except ValueError:
        price = 0

    per = pbr = eps = market_cap = 0.0
    for info in data.get("stockItemTotalInfos", []):
        code_key = info.get("code")
        if code_key == "per":
            per = _parse_ratio(info.get("value"))
        elif code_key == "pbr":
            pbr = _parse_ratio(info.get("value"))
        elif code_key == "eps":
            eps = _parse_ratio(info.get("value"))
        elif code_key == "marketValue":
            market_cap = _parse_ratio(info.get("value"))

    return {
        "종목명": name,
        "현재가": price,
        "PER": per,
        "PBR": pbr,
        "ROE": 0,  # 데이터 없음 -> fundamental_score()가 중립 처리
        "EPS": eps,
        "시가총액": market_cap,
    }


def _index_snapshot(code: str):
    try:
        resp = requests.get(INDEX_URL.format(code=code), headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[네이버 해외지수 오류] {code}: {e}")
        return None

    try:
        value = float(str(data["closePrice"]).replace(",", ""))
        change_pct = float(data.get("fluctuationsRatio", 0))
        up = data.get("compareToPreviousPrice", {}).get("text") != "하락"
    except (KeyError, ValueError, TypeError):
        return None
    return {"value": value, "change_pct": change_pct, "up": up}


def get_market_indices():
    """대시보드 히어로 카드용 S&P500/나스닥 지수. 5분 캐시, 실패 시 이전 값(또는 None) 유지."""
    now = time.time()
    if _index_cache["data"] is not None and now - _index_cache["fetched_at"] < _INDEX_CACHE_TTL_SEC:
        return _index_cache["data"]

    result = {}
    for name, code in (("SP500", ".INX"), ("NASDAQ", ".IXIC")):
        parsed = _index_snapshot(code)
        if parsed:
            result[name] = parsed

    if result:
        _index_cache["data"] = result
        _index_cache["fetched_at"] = now
        return result
    return _index_cache["data"]
