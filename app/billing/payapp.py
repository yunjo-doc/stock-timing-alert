# -*- coding: utf-8 -*-
"""PayApp(페이앱) 정기결제 연동

Toss/카카오페이와 결정적으로 다른 점: 우리 서버가 매 결제 주기마다 "청구" API를
호출하지 않습니다. 최초 1회 등록(rebillRegist) 후 사용자가 payurl에서 결제를
승인하면, 그 뒤로는 PayApp이 알아서 주기적으로 자동 청구하고 매번 feedbackurl로
결과를 통보합니다. 우리는 그 통보를 받아 구독 기간을 연장하기만 하면 됩니다.

흐름:
  1) register_subscription() 으로 정기결제 등록 -> rebill_no, payurl 발급
  2) 사용자가 payurl에서 최초 결제 승인(카드/휴대전화 등록 겸 첫 결제)
  3) 최초 결제 및 이후 매 결제 주기마다 feedbackurl(POST)로 통보 (rebill_no 포함)
  4) cancel_subscription() / pause_subscription() / resume_subscription() 으로 관리

문서: PayAPP-연동API-oapi-0044.docx "3. 정기 결제 연동", "2.4 결제통보(FeedbackURL)"
"""

import os
from urllib.parse import parse_qsl

import requests

API_URL = "https://api.payapp.kr/oapi/apiLoad.html"


def _userid() -> str:
    return os.getenv("PAYAPP_USERID", "")


def _linkkey() -> str:
    return os.getenv("PAYAPP_LINKKEY", "")


def linkval() -> str:
    """feedbackurl로 들어오는 linkval과 대조해 PayApp이 보낸 요청이 맞는지 검증할 때 씁니다."""
    return os.getenv("PAYAPP_LINKVAL", "")


def is_configured() -> bool:
    return bool(_userid() and _linkkey())


class PayAppError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def _call(payload: dict) -> dict:
    body = {"userid": _userid(), **payload}
    resp = requests.post(API_URL, data=body, timeout=15)
    resp.encoding = "utf-8"
    parsed = dict(parse_qsl(resp.text))
    if parsed.get("state") != "1":
        raise PayAppError(parsed.get("errorMessage") or "PayApp 요청 처리 중 오류가 발생했습니다.",
                           response=parsed)
    return parsed


def register_subscription(goodname: str, goodprice: int, recvphone: str, cycle_day: int,
                           expire_date: str, feedbackurl: str, failurl: str = "",
                           var1: str = "", var2: str = "") -> dict:
    """정기결제 등록. cycle_day: 매월 결제일(1~31, 90=말일). expire_date: 'yyyy-mm-dd'."""
    return _call({
        "cmd": "rebillRegist",
        "goodname": goodname,
        "goodprice": goodprice,
        "recvphone": recvphone,
        "rebillCycleType": "Month",
        "rebillCycleMonth": cycle_day,
        "rebillExpire": expire_date,
        "feedbackurl": feedbackurl,
        "failurl": failurl,
        "var1": var1,
        "var2": var2,
        "smsuse": "n",
    })


def cancel_subscription(rebill_no: str) -> dict:
    return _call({"cmd": "rebillCancel", "rebill_no": rebill_no, "linkkey": _linkkey()})


def pause_subscription(rebill_no: str) -> dict:
    return _call({"cmd": "rebillStop", "rebill_no": rebill_no, "linkkey": _linkkey()})


def resume_subscription(rebill_no: str) -> dict:
    return _call({"cmd": "rebillStart", "rebill_no": rebill_no, "linkkey": _linkkey()})
