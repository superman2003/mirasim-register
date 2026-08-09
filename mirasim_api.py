"""Mirasim / Mirofish auth protocol client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_LOGIN_BASE = "https://admin.test.mirofish.ai"
DEFAULT_TIMEOUT = 30.0


class MirasimApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, reason: str = "http"):
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"access_token": self.access_token}
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        return out


@dataclass
class ReferralInfo:
    code: str | None
    redeemed: int = 0
    threshold: int = 0
    remaining: int = 0
    reached: bool = False
    max_redemptions: int = 0
    current_plan: str = ""
    next_plan: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "redeemed": self.redeemed,
            "threshold": self.threshold,
            "remaining": self.remaining,
            "reached": self.reached,
            "max_redemptions": self.max_redemptions,
            "current_plan": self.current_plan,
            "next_plan": self.next_plan,
        }


class MirasimClient:
    def __init__(self, login_base: str = DEFAULT_LOGIN_BASE, timeout: float = DEFAULT_TIMEOUT):
        self.login_base = (login_base or DEFAULT_LOGIN_BASE).rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.login_base}{path}"

    def _raise_for_response(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        detail = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = str(data.get("detail") or data.get("message") or data.get("error") or "")
        except Exception:
            detail = (resp.text or "")[:240]
        msg = detail or f"HTTP {resp.status_code}"
        raise MirasimApiError(msg, status=resp.status_code)

    @staticmethod
    def _parse_tokens(data: dict[str, Any], *, label: str) -> AuthTokens:
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise MirasimApiError(f"{label} response carried no access_token", reason="bad-response")
        refresh = data.get("refresh_token")
        return AuthTokens(
            access_token=token,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        )

    @staticmethod
    def _parse_referral(data: dict[str, Any]) -> ReferralInfo:
        def num(key: str) -> int:
            v = data.get(key)
            return int(v) if isinstance(v, (int, float)) else 0

        code = data.get("code")
        return ReferralInfo(
            code=code if isinstance(code, str) and code else None,
            redeemed=num("redeemed"),
            threshold=num("threshold"),
            remaining=num("remaining"),
            reached=data.get("reached") is True,
            max_redemptions=num("max_redemptions"),
            current_plan=str(data.get("current_plan") or ""),
            next_plan=str(data.get("next_plan") or ""),
        )

    def send_code(self, email: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._url("/auth/code"),
                headers={"content-type": "application/json"},
                json={"email": email},
            )
            self._raise_for_response(resp)
            data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            data = {}
        out: dict[str, Any] = {"ok": True}
        if isinstance(data.get("dev_code"), str) and data["dev_code"]:
            out["dev_code"] = data["dev_code"]
        return out

    def verify_code(self, email: str, code: str) -> AuthTokens:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._url("/auth/verify"),
                headers={"content-type": "application/json"},
                json={"email": email, "code": code},
            )
            self._raise_for_response(resp)
            data = resp.json()
        if not isinstance(data, dict):
            raise MirasimApiError("verify response is not JSON object", reason="bad-response")
        return self._parse_tokens(data, label="verify")

    def redeem_invite(self, access_token: str, invite_code: str) -> AuthTokens:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._url("/auth/invite/redeem"),
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {access_token}",
                },
                json={"code": invite_code.strip()},
            )
            self._raise_for_response(resp)
            data = resp.json()
        if not isinstance(data, dict):
            raise MirasimApiError("redeem response is not JSON object", reason="bad-response")
        return self._parse_tokens(data, label="redeem")

    def get_me(self, access_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                self._url("/auth/me"),
                headers={"authorization": f"Bearer {access_token}"},
            )
            if not resp.is_success:
                return {}
            data = resp.json()
        return data if isinstance(data, dict) else {}

    def get_referral(self, access_token: str) -> ReferralInfo | None:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                self._url("/auth/referral"),
                headers={"authorization": f"Bearer {access_token}"},
            )
            if not resp.is_success:
                return None
            data = resp.json()
        if not isinstance(data, dict):
            return None
        return self._parse_referral(data)

    def create_referral(self, access_token: str) -> ReferralInfo:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self._url("/auth/referral"),
                headers={"authorization": f"Bearer {access_token}"},
            )
            self._raise_for_response(resp)
            data = resp.json()
        if not isinstance(data, dict):
            raise MirasimApiError("referral response is not JSON object", reason="bad-response")
        info = self._parse_referral(data)
        if not info.code:
            raise MirasimApiError("referral response carried no code", reason="bad-response")
        return info

    def ensure_referral(self, access_token: str) -> ReferralInfo:
        existing = self.get_referral(access_token)
        if existing and existing.code:
            return existing
        return self.create_referral(access_token)
