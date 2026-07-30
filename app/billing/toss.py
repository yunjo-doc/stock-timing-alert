# -*- coding: utf-8 -*-
"""Toss Payments 빌링(정기결제) 연동

흐름:
  1) 프론트에서 TossPayments JS SDK로 카드 등록 -> authKey, customerKey 를 받아 서버로 전달
  2) issue_billing_key() 로 authKey -> billingKey 교환 (최초 1회, 카드 저장)
  3) charge_billing() 으로 billingKey를 이용해 최초 결제 및 매월 자동 결제 청구

문서: https://docs.tosspayments.com/guides/v2/billing/integration
"""

import base64
import os

import requests

API_BASE = "https://api.tosspayments.com/v1"


def _secret_key() -> str:
    return os.getenv("TOSS_SECRET_KEY", "")


def _auth_header() -> dict:
    token = base64.b64encode(f"{_secret_key()}:".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


class TossError(Exception):
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


def _post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{API_BASE}{path}", json=payload, headers=_auth_header(), timeout=15)
    data = resp.json()
    if resp.status_code >= 400:
        raise TossError(data.get("message", "결제 요청 처리 중 오류가 발생했습니다."),
                         code=data.get("code"), response=data)
    return data


def issue_billing_key(auth_key: str, customer_key: str) -> dict:
    """카드 등록 인증 완료 후 authKey를 billingKey로 교환합니다."""
    return _post("/billing/authorizations/issue", {
        "authKey": auth_key,
        "customerKey": customer_key,
    })


def charge_billing(billing_key: str, customer_key: str, amount: int, order_id: str, order_name: str) -> dict:
    """저장된 billingKey로 결제를 청구합니다 (최초 결제 및 매월 자동 결제 공통)."""
    return _post(f"/billing/{billing_key}", {
        "customerKey": customer_key,
        "amount": amount,
        "orderId": order_id,
        "orderName": order_name,
    })
