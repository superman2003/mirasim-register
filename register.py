"""Single / batch Mirasim registration with invite chain."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mail_otp import build_mail_provider
from mirasim_api import AuthTokens, MirasimApiError, MirasimClient

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


@dataclass
class RegisterResult:
    ok: bool
    email: str = ""
    invite_used: str | None = None
    invite_created: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    referral: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(fn: LogFn | None, msg: str) -> None:
    logger.info(msg)
    if fn:
        fn(msg)


def register_one(
    *,
    imap_cfg: dict[str, Any],
    login_base: str,
    invite_code: str | None = None,
    email: str | None = None,
    otp_timeout: int = 120,
    create_invite: bool = True,
    log: LogFn | None = None,
) -> RegisterResult:
    client = MirasimClient(login_base=login_base)
    mail = build_mail_provider(imap_cfg)

    if not (imap_cfg.get("email") and imap_cfg.get("auth_code")):
        return RegisterResult(ok=False, error="缺少 QQ IMAP 邮箱或授权码")
    if not imap_cfg.get("catch_all_domain") and not email:
        return RegisterResult(ok=False, error="缺少 catch_all_domain，且未指定邮箱")

    try:
        target_email = (email or "").strip() or mail.create_mailbox()
        _log(log, f"使用邮箱: {target_email}")

        issued_after = time.time()
        send_resp = client.send_code(target_email)
        if send_resp.get("dev_code"):
            _log(log, f"服务端返回 dev_code: {send_resp['dev_code']}")
            otp = str(send_resp["dev_code"])
        else:
            _log(log, "已请求发送验证码，开始 IMAP 收信…")
            otp = mail.wait_for_otp(target_email, timeout=otp_timeout, issued_after=issued_after)
        _log(log, f"验证码: {otp}")

        tokens: AuthTokens = client.verify_code(target_email, otp)
        _log(log, "邮箱验证成功，已拿到 access_token")

        invite_used = (invite_code or "").strip() or None
        if invite_used:
            _log(log, f"兑换邀请码: {invite_used}")
            tokens = client.redeem_invite(tokens.access_token, invite_used)
            _log(log, "邀请码兑换成功，token 已刷新")

        profile = client.get_me(tokens.access_token)
        if profile:
            _log(log, f"账号资料: {profile.get('email') or target_email} / {profile.get('name') or '-'}")

        invite_created = None
        referral: dict[str, Any] = {}
        if create_invite:
            info = client.ensure_referral(tokens.access_token)
            referral = info.as_dict()
            invite_created = info.code
            _log(log, f"邀请码就绪: {invite_created}")

        return RegisterResult(
            ok=True,
            email=target_email,
            invite_used=invite_used,
            invite_created=invite_created,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            profile=profile,
            referral=referral,
            created_at=_now_iso(),
        )
    except MirasimApiError as e:
        _log(log, f"API 失败: {e}")
        return RegisterResult(ok=False, email=email or "", invite_used=invite_code, error=str(e), created_at=_now_iso())
    except Exception as e:
        _log(log, f"注册失败: {e}")
        return RegisterResult(ok=False, email=email or "", invite_used=invite_code, error=str(e), created_at=_now_iso())


def _invite_quota(result: RegisterResult, default_uses: int) -> int:
    """How many times a freshly created invite should be reused."""
    ref = result.referral or {}
    for key in ("remaining", "threshold"):
        try:
            n = int(ref.get(key) or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
    return max(1, int(default_uses))


def register_batch(
    *,
    imap_cfg: dict[str, Any],
    login_base: str,
    count: int,
    seed_invite: str | None = None,
    otp_timeout: int = 120,
    create_invite: bool = True,
    stop_on_error: bool = True,
    invite_uses_per_code: int = 10,
    log: LogFn | None = None,
    on_account: Callable[[RegisterResult], None] | None = None,
) -> list[RegisterResult]:
    """Batch register with invite reuse.

    Each invite code is reused up to ``invite_uses_per_code`` times (default 10,
    or referral.remaining/threshold when known). Newly generated invites are
    queued and only become active after the current code is exhausted.
    """
    results: list[RegisterResult] = []
    default_uses = max(1, int(invite_uses_per_code or 10))

    current_invite = (seed_invite or "").strip() or None
    uses_left = default_uses if current_invite else 0
    # queue of (code, uses_left) produced by successful signups
    invite_queue: list[tuple[str, int]] = []
    seen_codes: set[str] = set()
    if current_invite:
        seen_codes.add(current_invite)

    for i in range(max(1, int(count))):
        # If no active invite, pull from queue (e.g. first account minted one)
        if not current_invite and invite_queue:
            current_invite, uses_left = invite_queue.pop(0)
            _log(log, f"启用队列邀请码: {current_invite} (可再用不超 {uses_left} 次)")

        _log(log, f"======== 第 {i + 1}/{count} 个 ========")
        if current_invite:
            _log(log, f"本轮邀请码: {current_invite} (剩余计划使用 {uses_left} 次)")
        else:
            _log(log, "本轮无邀请码（仅邮箱登录；成功后会生成邀请码入队）")

        result = register_one(
            imap_cfg=imap_cfg,
            login_base=login_base,
            invite_code=current_invite,
            otp_timeout=otp_timeout,
            create_invite=create_invite,
            log=log,
        )
        results.append(result)
        if on_account:
            on_account(result)

        if not result.ok:
            # Redeem failures (code used up / invalid): rotate to next queued invite
            err = (result.error or "").lower()
            if current_invite and any(
                k in err for k in ("invite", "redeem", "code", "额度", "次数", "无效", "expired")
            ):
                _log(log, f"邀请码疑似失效，丢弃: {current_invite}")
                current_invite = None
                uses_left = 0
            if stop_on_error and not invite_queue and not current_invite:
                _log(log, "遇到错误且无可用邀请码，停止批量")
                break
            if stop_on_error and current_invite:
                _log(log, "遇到错误，停止批量")
                break
            continue

        # Enqueue newly minted invite for later reuse (do NOT switch immediately)
        if create_invite and result.invite_created:
            code = result.invite_created.strip()
            if code and code not in seen_codes:
                quota = _invite_quota(result, default_uses)
                invite_queue.append((code, quota))
                seen_codes.add(code)
                _log(log, f"新邀请码入队: {code} (可用 {quota} 次，队列长度 {len(invite_queue)})")

        if current_invite and result.invite_used:
            uses_left -= 1
            _log(log, f"邀请码 {current_invite} 本批已用，剩余计划 {max(0, uses_left)} 次")
            if uses_left <= 0:
                _log(log, f"邀请码已用满计划次数，准备切换: {current_invite}")
                current_invite = None
                uses_left = 0

        time.sleep(1.2)

    return results


def append_accounts(path: Path, results: list[RegisterResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except Exception:
            existing = []
    for r in results:
        if r.ok:
            existing.append(r.as_dict())
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
