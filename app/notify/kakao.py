# -*- coding: utf-8 -*-
"""
카카오톡 알림 모듈 (웹앱용)

config['kakao']['enabled'] (환경변수 KAKAO_ENABLED=true) 가 켜져 있으면
실제 "나에게 보내기" API로 전송하고, 꺼져 있으면 콘솔 출력 + DB 로그로 대체합니다.

연동 방법은 README.md 의 "카카오톡 연동" 섹션을 참고하세요.
"""

import json
import requests

from .. import db

KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_USER_ME_URL = "https://kapi.kakao.com/v2/user/me"


def get_authorize_url(rest_api_key: str, redirect_uri: str, scope: str = "talk_message") -> str:
    # prompt=login 을 주지 않으면 카카오 로그인 세션이 남아있는 경우 동의 화면 자체를
    # 건너뛰고 예전에 허용했던 스코프 그대로 즉시 리다이렉트해버려서, 연동 해제 후 재연동해도
    # talk_message 동의 체크박스를 다시 볼 기회가 없이 바로 "연결됨"으로 넘어가는 문제가 있었습니다.
    return (f"{KAKAO_AUTH_URL}?client_id={rest_api_key}"
            f"&redirect_uri={redirect_uri}&response_type=code&scope={scope}&prompt=login")


def get_user_profile(access_token: str) -> dict:
    resp = requests.get(KAKAO_USER_ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    return resp.json()


def exchange_code_for_token(rest_api_key: str, code: str, redirect_uri: str, client_secret: str = "") -> dict:
    payload = {
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    resp = requests.post(KAKAO_TOKEN_URL, data=payload)
    if resp.status_code >= 400:
        # 카카오가 돌려주는 error/error_description을 그대로 노출해야 원인(시크릿 불일치,
        # 리다이렉트 URI 불일치, 코드 재사용 등)을 화면에서 바로 확인할 수 있습니다.
        raise RuntimeError(f"카카오 토큰 발급 실패 ({resp.status_code}): {resp.text}")
    return resp.json()


def refresh_kakao_token(rest_api_key: str, refresh_token: str, client_secret: str = "") -> dict:
    payload = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    resp = requests.post(KAKAO_TOKEN_URL, data=payload)
    resp.raise_for_status()
    return resp.json()


SITE_URL = "https://alphaone.ai.kr"


def _send_kakao_memo(access_token: str, text: str) -> bool:
    # 모든 메시지 종류(전환 알림/일일 요약/테스트)에 공통으로 사이트 링크를 붙여서,
    # 메시지를 눌렀을 때 바로 alphaone.ai.kr로 들어올 수 있게 합니다.
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": SITE_URL, "mobile_web_url": SITE_URL},
        "button_title": "AlphaTiming 바로가기",
    }
    resp = requests.post(
        KAKAO_SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
    )
    return resp.status_code == 200


def _send_with_refresh(user: dict, message: str, rest_api_key: str, client_secret: str) -> bool:
    """access_token으로 전송을 시도하고, 실패하면(만료 가능성) refresh_token으로 한 번
    갱신 후 재시도한다. 성공 시 갱신된 토큰을 DB에 저장한다."""
    sent = False
    try:
        sent = _send_kakao_memo(user["kakao_access_token"], message)
        if not sent and rest_api_key and user.get("kakao_refresh_token"):
            refreshed = refresh_kakao_token(rest_api_key, user["kakao_refresh_token"], client_secret)
            new_access = refreshed.get("access_token")
            new_refresh = refreshed.get("refresh_token", user["kakao_refresh_token"])
            if new_access:
                db.save_user_kakao_tokens(user["id"], new_access, new_refresh)
                sent = _send_kakao_memo(new_access, message)
    except Exception as e:
        print(f"[카카오톡 전송 오류] user_id={user['id']}: {e}")
    return sent


def notify_all_connected_users(code: str, message: str, cfg: dict):
    """
    카카오 '나에게 보내기'를 연동하고, 해당 종목을 관심종목으로 등록한 사용자에게만 알림을 보낸다.
    (관심종목 등록 개수로 과금하는 구조이므로, 등록하지 않은 종목의 신호까지 받으면 안 됩니다.)
    access_token이 만료된 경우(401) refresh_token으로 한 번 자동 갱신 후 재시도한다.
    연동+등록한 사용자가 한 명도 없으면 콘솔 로그로 대체한다.
    """
    users = db.get_kakao_connected_users_watching(code)

    if not users:
        print(f"[알림 - 카카오 연동 사용자 없음, 로그 대체]\n{message}\n")
        db.log_notification(code, message, False)
        return False

    any_sent = False
    for user in users:
        sent = notify_user(user, code, message, cfg)
        any_sent = any_sent or sent

    return any_sent


def notify_user(user: dict, code: str, message: str, cfg: dict) -> bool:
    """특정 회원 한 명에게 메시지를 보내고 결과를 알림 이력에 남긴다 (일일 요약 등에서 사용)."""
    rest_api_key = cfg.get("kakao", {}).get("rest_api_key")
    client_secret = cfg.get("kakao", {}).get("client_secret", "")
    sent = _send_with_refresh(user, message, rest_api_key, client_secret)
    db.log_notification(code, message, sent, user_id=user["id"])
    return sent


def send_test_message(user: dict, cfg: dict) -> bool:
    """연동 화면에서 '테스트 알림 보내기'로 호출 — 이 사용자 한 명에게만 발송한다."""
    message = "[AlphaTiming 테스트] 카카오톡 알림 연동이 정상적으로 완료되었습니다."
    return notify_user(user, "TEST", message, cfg)
