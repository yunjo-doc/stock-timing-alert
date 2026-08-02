# -*- coding: utf-8 -*-
"""세션 기반 로그인 상태 확인 헬퍼"""

from datetime import datetime

from fastapi import Request
from . import db

# 1시간 동안 요청이 없으면 세션을 만료시킵니다(활동이 있을 때마다 아래에서 갱신되는
# sliding window 방식 - 로그인 후 1시간 뒤가 아니라, 마지막 활동 후 1시간입니다).
SESSION_IDLE_TIMEOUT_SECONDS = 60 * 60


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    last_seen = request.session.get("last_seen")
    if last_seen is not None and (datetime.now().timestamp() - last_seen) > SESSION_IDLE_TIMEOUT_SECONDS:
        request.session.clear()
        return None
    request.session["last_seen"] = datetime.now().timestamp()

    user = db.get_user_by_id(user_id)
    if user and user.get("is_active") == 0:
        return None  # 관리자가 정지시킨 계정 -> 즉시 로그아웃 상태로 취급
    return user


def login_user(request: Request, user_id: int):
    request.session["user_id"] = user_id
    request.session["last_seen"] = datetime.now().timestamp()


def logout_user(request: Request):
    request.session.clear()


def needs_profile_completion(user: dict) -> bool:
    """카카오 로그인은 닉네임 동의만 받고 이름/전화번호를 요청하지 않아서,
    가입 직후 이 값들이 비어있는 계정을 "쓰레기 데이터" 없이 채우도록 강제할 때 씁니다."""
    return not (user.get("name") and user.get("phone"))
