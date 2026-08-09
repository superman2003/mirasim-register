"""Mirasim registration panel — QQ IMAP catch-all + invite chain."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mirasim_api import DEFAULT_LOGIN_BASE, MirasimClient
from register import RegisterResult, append_accounts, register_batch, register_one

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ACCOUNTS_PATH = BASE_DIR / "data" / "accounts.json"
HTML_PATH = BASE_DIR / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mirasim-reg")

app = FastAPI(title="Mirasim Reg", version="0.1.0")

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()


def _default_config() -> dict[str, Any]:
    return {
        "login_base": DEFAULT_LOGIN_BASE,
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "email": "",
        "auth_code": "",
        "catch_all_domain": "",
        "seed_invite": "",
        "otp_timeout": 120,
    }


def load_config() -> dict[str, Any]:
    cfg = _default_config()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception as e:
            logger.warning("读取 config.json 失败: %s", e)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    merged = _default_config()
    merged.update(cfg)
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def load_accounts() -> list[dict[str, Any]]:
    if not ACCOUNTS_PATH.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _imap_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "imap_server": cfg.get("imap_server") or "imap.qq.com",
        "imap_port": int(cfg.get("imap_port") or 993),
        "email": (cfg.get("email") or "").strip(),
        "auth_code": (cfg.get("auth_code") or "").strip(),
        "catch_all_domain": (cfg.get("catch_all_domain") or "").strip(),
    }


class ConfigBody(BaseModel):
    login_base: str = DEFAULT_LOGIN_BASE
    imap_server: str = "imap.qq.com"
    imap_port: int = 993
    email: str = ""
    auth_code: str = ""
    catch_all_domain: str = ""
    seed_invite: str = ""
    otp_timeout: int = Field(default=120, ge=30, le=600)


class RegisterBody(BaseModel):
    count: int = Field(default=1, ge=1, le=50)
    seed_invite: str | None = None
    login_base: str | None = None
    email: str | None = None
    otp_timeout: int | None = Field(default=None, ge=30, le=600)
    create_invite: bool = True
    stop_on_error: bool = True
    # optional one-shot IMAP overrides
    imap_server: str | None = None
    imap_port: int | None = None
    imap_email: str | None = None
    auth_code: str | None = None
    catch_all_domain: str | None = None


class RedeemBody(BaseModel):
    access_token: str
    invite_code: str
    login_base: str | None = None


class ReferralBody(BaseModel):
    access_token: str
    login_base: str | None = None
    create: bool = True


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not HTML_PATH.exists():
        return HTMLResponse("<h3>index.html 缺失</h3>", status_code=404)
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    cfg = load_config()
    # mask auth_code a bit for display safety in UI still needs full value for editing —
    # return full local config; this is a local tool.
    return {"ok": True, "config": cfg}


@app.post("/api/config")
async def api_save_config(body: ConfigBody) -> dict[str, Any]:
    save_config(body.model_dump())
    return {"ok": True}


@app.get("/api/accounts")
async def api_accounts() -> dict[str, Any]:
    items = load_accounts()
    return {"ok": True, "count": len(items), "items": list(reversed(items))}


@app.post("/api/register")
async def api_register(body: RegisterBody) -> dict[str, Any]:
    cfg = load_config()
    if body.login_base:
        cfg["login_base"] = body.login_base.strip()
    if body.otp_timeout:
        cfg["otp_timeout"] = body.otp_timeout
    if body.imap_server:
        cfg["imap_server"] = body.imap_server
    if body.imap_port:
        cfg["imap_port"] = body.imap_port
    if body.imap_email:
        cfg["email"] = body.imap_email
    if body.auth_code:
        cfg["auth_code"] = body.auth_code
    if body.catch_all_domain:
        cfg["catch_all_domain"] = body.catch_all_domain

    seed = (body.seed_invite if body.seed_invite is not None else cfg.get("seed_invite") or "") or ""
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "logs": [],
            "accounts": [],
            "created_at": time.time(),
            "done": False,
        }

    def push(msg: str) -> None:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t is not None:
                t["logs"].append({"ts": time.time(), "msg": msg})

    def on_account(result: RegisterResult) -> None:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t is not None:
                t["accounts"].append(result.as_dict())
        if result.ok:
            append_accounts(ACCOUNTS_PATH, [result])

    def worker() -> None:
        try:
            push(f"任务启动 count={body.count} base={cfg.get('login_base')}")
            if body.count == 1 and body.email:
                result = register_one(
                    imap_cfg=_imap_cfg(cfg),
                    login_base=str(cfg.get("login_base") or DEFAULT_LOGIN_BASE),
                    invite_code=seed or None,
                    email=body.email,
                    otp_timeout=int(cfg.get("otp_timeout") or 120),
                    create_invite=body.create_invite,
                    log=push,
                )
                on_account(result)
                results = [result]
            else:
                results = register_batch(
                    imap_cfg=_imap_cfg(cfg),
                    login_base=str(cfg.get("login_base") or DEFAULT_LOGIN_BASE),
                    count=body.count,
                    seed_invite=seed or None,
                    otp_timeout=int(cfg.get("otp_timeout") or 120),
                    create_invite=body.create_invite,
                    stop_on_error=body.stop_on_error,
                    log=push,
                    on_account=on_account,
                )
            ok_n = sum(1 for r in results if r.ok)
            push(f"完成: 成功 {ok_n}/{len(results)}")
            with _tasks_lock:
                t = _tasks[task_id]
                t["status"] = "done"
                t["done"] = True
                t["summary"] = {"ok": ok_n, "total": len(results)}
        except Exception as e:
            logger.exception("register task failed")
            push(f"任务异常: {e}")
            with _tasks_lock:
                t = _tasks[task_id]
                t["status"] = "error"
                t["done"] = True
                t["error"] = str(e)

    threading.Thread(target=worker, name=f"mirasim-reg-{task_id}", daemon=True).start()
    return {"ok": True, "task_id": task_id}


@app.get("/api/stream/{task_id}")
async def api_stream_task(task_id: Annotated[str, ApiPath()]) -> EventSourceResponse:
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(status_code=404, detail="task not found")

    async def event_gen():
        cursor = 0
        acc_cursor = 0
        while True:
            with _tasks_lock:
                task = _tasks.get(task_id)
                if task is None:
                    yield {
                        "event": "error",
                        "data": json.dumps({"message": "missing"}, ensure_ascii=False),
                    }
                    return
                logs = task["logs"]
                accounts = task.get("accounts") or []
                new_logs = logs[cursor:]
                new_acc = accounts[acc_cursor:]
                cursor = len(logs)
                acc_cursor = len(accounts)
                done = bool(task.get("done"))
                status = task.get("status")
                summary = task.get("summary")
            for item in new_logs:
                yield {"event": "log", "data": json.dumps(item, ensure_ascii=False)}
            for acc in new_acc:
                yield {"event": "account", "data": json.dumps(acc, ensure_ascii=False)}
            if done:
                yield {
                    "event": "done",
                    "data": json.dumps({"status": status, "summary": summary}, ensure_ascii=False),
                }
                return
            await asyncio.sleep(0.4)

    return EventSourceResponse(event_gen())


@app.post("/api/redeem")
async def api_redeem(body: RedeemBody) -> dict[str, Any]:
    cfg = load_config()
    base = (body.login_base or cfg.get("login_base") or DEFAULT_LOGIN_BASE).rstrip("/")
    client = MirasimClient(login_base=base)
    try:
        tokens = client.redeem_invite(body.access_token.strip(), body.invite_code.strip())
        referral = client.ensure_referral(tokens.access_token)
        return {
            "ok": True,
            "tokens": tokens.as_dict(),
            "referral": referral.as_dict(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/referral")
async def api_referral(body: ReferralBody) -> dict[str, Any]:
    cfg = load_config()
    base = (body.login_base or cfg.get("login_base") or DEFAULT_LOGIN_BASE).rstrip("/")
    client = MirasimClient(login_base=base)
    try:
        if body.create:
            info = client.ensure_referral(body.access_token.strip())
        else:
            info = client.get_referral(body.access_token.strip())
            if info is None:
                return {"ok": False, "error": "no referral"}
        return {"ok": True, "referral": info.as_dict()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    import uvicorn

    port = int(__import__("os").environ.get("MIRASIM_REG_PORT", "8788"))
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
