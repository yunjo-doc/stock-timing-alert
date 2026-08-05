# -*- coding: utf-8 -*-
"""카카오페이 정기결제 연동

흐름:
  1) ready_subscription() 으로 결제 준비 요청 -> next_redirect_pc_url로 사용자를 리디렉션
  2) 사용자가 카카오페이에서 결제 수단 인증을 마치면 approval_url로 리디렉션(pg_token 포함)
  3) approve_subscription() 으로 pg_token -> sid(정기결제 고유번호) 교환 (최초 1회, 결제수단 저장 겸 첫 결제)
  4) charge_subscription() 으로 저장된 sid를 이용해 매월 자동 결제 청구

문서: https://developers.kakaopay.com/docs/payment/online/subscription
"""

import os

import requests

API_BASE = "https://open-api.kakaopay.com/online/v1/payment"

# ready()에서 발급받은 tid는 approve() 호출 때 다시 필요한데, 카카오페이는 리디렉션 URL에
# 우리가 넘긴 값 그대로 pg_token만 추가해서 돌려주기 때문에 tid를 따로 들고 있어야 합니다.
# 단일 프로세스로 도는 소규모 서비스라 프로세스 메모리에 짧게(결제 진행 몇 분 동안만) 보관합니다.
_PENDING_TID: dict[str, str] = {}


def _cid() -> str:
    return os.getenv("KAKAO_PAY_CID", "")


def _auth_header() -> dict:
    secret = os.getenv("KAKAO_PAY_SECRET_KEY", "")
    scheme = "SECRET_KEY"
    if not secret:
        secret = os.getenv("KAKAO_PAY_DEV_SECRET_KEY", "")
        scheme = "DEV_SECRET_KEY"
    return {"Authorization": f"{scheme} {secret}", "Content-Type": "application/json"}


def is_configured() -> bool:
    return bool(_cid() and (os.getenv("KAKAO_PAY_SECRET_KEY") or os.getenv("KAKAO_PAY_DEV_SECRET_KEY")))


class KakaoPayError(Exception):
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


def _post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{API_BASE}{path}", json=payload, headers=_auth_header(), timeout=15)
    data = resp.json()
    if resp.status_code >= 400:
        raise KakaoPayError(data.get("error_message", "결제 요청 처리 중 오류가 발생했습니다."),
                             code=data.get("error_code"), response=data)
    return data


def ready_subscription(order_id: str, partner_user_id: str, item_name: str, amount: int,
                        approval_url: str, fail_url: str, cancel_url: str) -> dict:
    """정기결제 1회차(결제수단 등록 겸 최초 결제)를 위한 결제 준비 요청."""
    data = _post("/ready", {
        "cid": _cid(),
        "partner_order_id": order_id,
        "partner_user_id": partner_user_id,
        "item_name": item_name,
        "quantity": 1,
        "total_amount": amount,
        "tax_free_amount": 0,
        "approval_url": approval_url,
        "fail_url": fail_url,
        "cancel_url": cancel_url,
    })
    _PENDING_TID[order_id] = data["tid"]
    return data


def approve_subscription(order_id: str, partner_user_id: str, pg_token: str) -> dict:
    """결제 준비 후 사용자가 인증을 완료하면 pg_token으로 결제를 승인하고 sid(정기결제 고유번호)를 발급받습니다."""
    tid = _PENDING_TID.pop(order_id, None)
    if not tid:
        raise KakaoPayError("결제 준비 정보를 찾을 수 없습니다. 다시 시도해주세요.")
    return _post("/approve", {
        "cid": _cid(),
        "tid": tid,
        "partner_order_id": order_id,
        "partner_user_id": partner_user_id,
        "pg_token": pg_token,
    })


def charge_subscription(sid: str, order_id: str, partner_user_id: str, item_name: str, amount: int) -> dict:
    """저장된 sid로 자동결제를 청구합니다 (2회차 이후 정기결제)."""
    return _post("/subscription", {
        "cid": _cid(),
        "sid": sid,
        "partner_order_id": order_id,
        "partner_user_id": partner_user_id,
        "item_name": item_name,
        "quantity": 1,
        "total_amount": amount,
        "tax_free_amount": 0,
    })


def inactivate_subscription(sid: str) -> dict:
    """정기결제를 비활성화(해지)합니다."""
    return _post("/manage/subscription/inactive", {
        "cid": _cid(),
        "sid": sid,
    })
