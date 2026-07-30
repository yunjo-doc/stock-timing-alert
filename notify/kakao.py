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


def get_authorize_url(rest_api_key: str, redirect_uri: str) -> str:
    return (f"{KAKAO_AUTH_URL}?client_id={rest_api_key}"
            f"&redirect_uri={redirect_uri}&response_type=code&scope=talk_message")


def exchange_code_for_token(rest_api_key: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(KAKAO_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    })
    resp.raise_for_status()
    return resp.json()


def refresh_kakao_token(rest_api_key: str, refresh_token: str) -> dict:
    resp = requests.post(KAKAO_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    })
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


def notify(code: str, message: str, cfg: dict):
    kakao_cfg = cfg.get("kakao", {})
    sent = False

    if kakao_cfg.get("enabled") and kakao_cfg.get("access_token"):
        try:
            sent = _send_kakao_memo(kakao_cfg["access_token"], message)
            if not sent:
                print("[카카오톡 전송 실패 - 토큰 만료 가능성, /kakao/refresh 확인]")
        except Exception as e:
            print(f"[카카오톡 전송 오류] {e}")
    else:
        print(f"[알림 - 카카오 미연동, 로그 대체]\n{message}\n")

    db.log_notification(code, message, sent)
    return sent
