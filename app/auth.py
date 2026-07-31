# -*- coding: utf-8 -*-
"""세션 기반 로그인 상태 확인 헬퍼"""

from fastapi import Request
from . import db


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get_user_by_id(user_id)
    if user and user.get("is_active") == 0:
        return None  # 관리자가 정지시킨 계정 -> 즉시 로그아웃 상태로 취급
    return user


def login_user(request: Request, user_id: int):
    request.session["user_id"] = user_id


def logout_user(request: Request):
    request.session.clear()
