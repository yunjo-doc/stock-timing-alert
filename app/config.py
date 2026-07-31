# -*- coding: utf-8 -*-
"""
설정 로더

- 종목/임계값/가중치 등 일반 설정 -> config.json
- 카카오 토큰 등 민감정보 -> 환경변수(.env)로 관리 (GitHub에 올라가면 안 되는 값들)
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
# Render 등에서 재배포 시에도 회원/DB 데이터가 사라지지 않도록, Persistent Disk를
# 마운트한 경로를 DATA_DIR 환경변수로 지정할 수 있습니다 (미설정 시 기존 동작 유지).
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 카카오 REST API 키는 앱 전체가 공유하는 자격증명이라 환경변수로 관리합니다.
    # 사용자별 access_token/refresh_token은 이제 DB(users 테이블)에 저장됩니다 (app/auth.py, /account 참고).
    cfg.setdefault("kakao", {})
    cfg["kakao"]["rest_api_key"] = os.getenv("KAKAO_REST_API_KEY", cfg["kakao"].get("rest_api_key", ""))
    # 카카오 개발자센터에서 "클라이언트 시크릿"을 활성화(ON)한 경우, 토큰 발급 요청에
    # client_secret을 함께 보내지 않으면 401(invalid_client) 오류가 발생합니다.
    cfg["kakao"]["client_secret"] = os.getenv("KAKAO_CLIENT_SECRET", cfg["kakao"].get("client_secret", ""))

    cfg["admin_token"] = os.getenv("ADMIN_TOKEN", "change-me")

    # Toss Payments 정기결제(빌링) 연동 키. TOSS_CLIENT_KEY는 프론트에 노출되는 공개키입니다.
    cfg.setdefault("toss", {})
    cfg["toss"]["client_key"] = os.getenv("TOSS_CLIENT_KEY", "")
    cfg["toss"]["secret_key_set"] = bool(os.getenv("TOSS_SECRET_KEY"))

    return cfg


def save_config(cfg: dict):
    """watch_list 등 일반 설정 변경사항을 config.json에 반영 (민감정보 제외하고 저장)"""
    safe_cfg = json.loads(json.dumps(cfg))  # deep copy
    safe_cfg.pop("admin_token", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(safe_cfg, f, ensure_ascii=False, indent=2)
