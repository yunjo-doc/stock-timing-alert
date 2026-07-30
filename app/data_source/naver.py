# -*- coding: utf-8 -*-
"""
네이버 금융(finance.naver.com) 비공식 크롤링 모듈

⚠️ 주의사항
- 이 모듈은 네이버가 공식 제공하는 API가 아니라, 네이버 금융 웹페이지의
  HTML 구조를 파싱하는 "비공식(unofficial)" 방식입니다.
- 네이버가 페이지 구조를 변경하면 파싱이 깨질 수 있습니다. 그럴 경우
  아래 정규식/선택자 부분을 그때의 페이지 구조에 맞게 수정해야 합니다.
- 개인적/비상업적 용도의 참고 지표 수집 목적으로만 사용하고,
  과도하게 짧은 주기로 대량 요청하지 마세요 (요청 사이 delay 포함됨).
- 네이버 금융 이용약관을 확인하시고, 상업적 서비스로 확장 시에는
  공식 유료 시세 데이터 제공업체(증권사 API, KRX 데이터 등) 사용을 권장합니다.
"""

import re
import time
from datetime import datetime

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}

REQUEST_DELAY_SEC = 0.4  # 네이버 서버 부담을 줄이기 위한 최소 대기시간


def _get(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    resp.encoding = "euc-kr"  # 네이버 금융은 euc-kr 인코딩 사용
    return resp.text


def get_daily_ohlcv(code: str, lookback_days: int = 120):
    """
    일별시세 페이지(sise_day.naver)를 페이지네이션하며 크롤링합니다.
    한 페이지당 10행이므로, lookback_days/10 만큼 페이지를 순회합니다.
    반환: [{"date","open","high","low","close","volume"}, ...] 과거->최근 순
    """
    rows = []
    pages_needed = (lookback_days // 10) + 2

    for page in range(1, pages_needed + 1):
        url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
        try:
            html = _get(url)
        except requests.RequestException as e:
            print(f"[네이버 크롤링 오류] {code} page={page}: {e}")
            break

        page_rows = _parse_sise_day(html)
        if not page_rows:
            break  # 더 이상 데이터 없음
        rows.extend(page_rows)
        time.sleep(REQUEST_DELAY_SEC)

        if len(rows) >= lookback_days:
            break

    rows = rows[:lookback_days]
    rows.reverse()  # 과거 -> 최근 순으로 정렬
    return rows


def _parse_sise_day(html: str):
    """
    sise_day.naver 테이블 구조: 날짜 | 종가 | 전일비 | 시가 | 고가 | 저가 | 거래량
    각 <tr> 블록을 통째로 뽑아 그 안의 날짜/숫자만 추출하는 방식(마크업 변경에 비교적 강건함).
    """
    rows = []
    tr_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    for block in tr_blocks:
        date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", block)
        if not date_match:
            continue
        nums = re.findall(r'<span class="tah p11">([\d,]+)</span>', block)
        if len(nums) < 5:
            continue
        try:
            close_p, open_p, high_p, low_p, volume = [int(n.replace(",", "")) for n in nums[:5]]
        except ValueError:
            continue
        rows.append({
            "date": date_match.group(1).replace(".", ""),
            "open": open_p, "high": high_p, "low": low_p,
            "close": close_p, "volume": volume,
        })
    return rows


def get_fundamental(code: str):
    """
    종목 메인 페이지(main.naver)에서 PER, PBR, ROE, 시가총액 등을 추출합니다.
    네이버 페이지 구조상 정확한 라벨 매칭이 어려운 경우가 있어,
    실패 시 0으로 채워 signal_engine 이 '중립' 처리하도록 합니다.
    """
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        html = _get(url)
    except requests.RequestException as e:
        print(f"[네이버 크롤링 오류] {code} 기본정보: {e}")
        return {"종목명": code, "현재가": 0, "PER": 0, "PBR": 0, "ROE": 0, "EPS": 0, "시가총액": 0}

    name_match = re.search(r'<title>(.*?)\s*:\s*네이버', html)
    name = name_match.group(1).strip() if name_match else code

    price_match = re.search(r'id="_nowVal">([\d,]+)</', html)
    price = int(price_match.group(1).replace(",", "")) if price_match else 0

    per = _extract_metric(html, "PER")
    pbr = _extract_metric(html, "PBR")
    roe = _extract_roe(html)
    market_cap = _extract_market_cap(html)

    return {
        "종목명": name,
        "현재가": price,
        "PER": per,
        "PBR": pbr,
        "ROE": roe,
        "EPS": 0,  # 필요 시 추가 파싱 가능
        "시가총액": market_cap,
    }


def _extract_metric(html: str, label: str) -> float:
    # 예: <th>PER<em class="...">l</em></th> ... <td><em>15.23</em></td> 형태를 방어적으로 탐색
    pattern = rf"{label}\s*(?:l|L)?\s*</th>\s*<td>\s*<em[^>]*>([\d,\.]+)</em>"
    m = re.search(pattern, html)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return 0.0
    return 0.0


def _extract_roe(html: str) -> float:
    # ROE는 페이지 하단 '기업실적분석' 표에 있어 파싱이 더 어려움 - 우선 0 처리,
    # 필요 시 별도 API(예: 공공데이터포털 기업 재무정보)로 보강 권장
    return 0.0


def _extract_market_cap(html: str) -> float:
    m = re.search(r"시가총액</th>\s*<td>\s*<em[^>]*>([\d,]+)</em>", html)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return 0.0
    return 0.0
