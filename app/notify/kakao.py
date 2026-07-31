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
    return (f"{KAKAO_AUTH_URL}?client_id={rest_api_key}"
            f"&redirect_uri={redirect_uri}&response_type=code&scope={scope}")


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
    resp.raise_for_status()
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


def _send_kakao_memo(access_token: str, text: str) -> bool:
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": "https://finance.naver.com", "mobile_web_url": "https://finance.naver.com"},
    }
    resp = requests.post(
        KAKAO_SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
    )
    return resp.status_code == 200


def notify_all_connected_users(code: str, message: str, cfg: dict):
    """
    카카오 '나에게 보내기'를 연동한 모든 사용자에게 알림을 보낸다.
    access_token이 만료된 경우(401) refresh_token으로 한 번 자동 갱신 후 재시도한다.
    연동한 사용자가 한 명도 없으면 콘솔 로그로 대체한다.
    """
    rest_api_key = cfg.get("kakao", {}).get("rest_api_key")
    users = db.get_all_kakao_connected_users()

    if not users:
        print(f"[알림 - 카카오 연동 사용자 없음, 로그 대체]\n{message}\n")
        db.log_notification(code, message, False)
        return False

    any_sent = False
    for user in users:
        sent = False
        try:
            sent = _send_kakao_memo(user["kakao_access_token"], message)
            if not sent and rest_api_key and user.get("kakao_refresh_token"):
                # 토큰 만료 가능성 -> 갱신 후 1회 재시도
                refreshed = refresh_kakao_token(rest_api_key, user["kakao_refresh_token"])
                new_access = refreshed.get("access_token")
                new_refresh = refreshed.get("refresh_token", user["kakao_refresh_token"])
                if new_access:
                    db.save_user_kakao_tokens(user["id"], new_access, new_refresh)
                    sent = _send_kakao_memo(new_access, message)
        except Exception as e:
            print(f"[카카오톡 전송 오류] user_id={user['id']}: {e}")

        db.log_notification(code, message, sent, user_id=user["id"])
        any_sent = any_sent or sent

    return any_sent
