"""QQ IMAP + catch-all domain OTP helper for Mirasim emails."""
from __future__ import annotations

import email
import email.message
import imaplib
import logging
import random
import re
import string
import time
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


class MirasimMailProvider:
    """IMAP catch-all mailbox generator + Mirasim OTP poller."""

    _GLOBAL_CONSUMED_UIDS: dict[str, set[int]] = {}

    def __init__(
        self,
        imap_server: str,
        imap_port: int,
        email_addr: str,
        auth_code: str,
        catch_all_domain: str = "",
    ):
        self.imap_server = imap_server
        self.imap_port = imap_port
        self.email_addr = email_addr
        self.auth_code = auth_code
        self.catch_all_domain = catch_all_domain
        global_key = f"{imap_server}:{imap_port}:{email_addr}".lower()
        self._consumed_uids = self._GLOBAL_CONSUMED_UIDS.setdefault(global_key, set())

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port, timeout=15)
        try:
            if getattr(conn, "sock", None):
                conn.sock.settimeout(15)
        except Exception:
            pass
        conn.login(self.email_addr, self.auth_code)
        return conn

    @staticmethod
    def _random_name() -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=5))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        return letters1 + numbers + letters2

    def create_mailbox(self) -> str:
        conn = self._connect()
        conn.logout()
        if self.catch_all_domain:
            addr = f"{self._random_name()}@{self.catch_all_domain}"
        else:
            addr = self.email_addr
        logger.info("mailbox ready: %s (imap inbox: %s)", addr, self.email_addr)
        return addr

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        parts = decode_header(value)
        out = []
        for chunk, charset in parts:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(chunk)
        return "".join(out)

    @staticmethod
    def _decode_payload(msg: email.message.Message) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body += payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
        return body

    @staticmethod
    def _extract_otp(content: str) -> str | None:
        text = (content or "").replace("\u00a0", " ")
        semantic_patterns = [
            r"(?:mirasim|mirofish)[^\n\r]{0,120}?(?:code|验证码)[^\d]{0,24}(\d{6})",
            r"(?:verification\s*code|one[-\s]*time\s*code|code\s*is|验证码(?:为|是)?)[^\d]{0,24}(\d{6})",
            r">\s*(\d{6})\s*<",
        ]
        for pattern in semantic_patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                return (m.group(1) or "").strip()
        scrubbed = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", " ", text)
        scrubbed = re.sub(r"https?://\S+", " ", scrubbed)
        candidates = re.findall(r"(?<![\w@.-])(\d{6})(?![\w@.-])", scrubbed)
        return candidates[-1] if candidates else None

    def _match_recipient(self, msg: email.message.Message, target_email: str) -> bool:
        if not target_email:
            return False
        target = target_email.lower().strip()
        headers_to_check = (
            "To",
            "Cc",
            "Delivered-To",
            "X-Original-To",
            "Envelope-To",
            "X-Envelope-To",
            "X-Forwarded-To",
            "X-Original-Recipient",
        )
        for header in headers_to_check:
            val = msg.get(header, "")
            if target in val.lower():
                return True
        all_headers = "\n".join(f"{k}: {v}" for k, v in msg.items()).lower()
        return target in all_headers

    @staticmethod
    def _message_timestamp(msg: email.message.Message) -> float | None:
        try:
            raw_date = msg.get("Date", "")
            if not raw_date:
                return None
            return parsedate_to_datetime(raw_date).timestamp()
        except Exception:
            return None

    @staticmethod
    def _search_uids(conn: imaplib.IMAP4_SSL, criteria: str) -> list[int]:
        try:
            status, data = conn.uid("search", None, criteria)
            if status != "OK" or not data or not data[0]:
                return []
            out: list[int] = []
            for raw in data[0].split():
                try:
                    out.append(int(raw))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    @staticmethod
    def _extract_internaldate_ts(fetch_meta) -> float | None:
        if not fetch_meta:
            return None
        try:
            text = fetch_meta.decode("utf-8", errors="replace") if isinstance(fetch_meta, bytes) else str(fetch_meta)
            m = re.search(r'INTERNALDATE\s+"([^"]+)"', text)
            if not m:
                return None
            dt = datetime.strptime(m.group(1), "%d-%b-%Y %H:%M:%S %z")
            return dt.timestamp()
        except Exception:
            return None

    def wait_for_otp(self, email_addr: str, timeout: int = 120, issued_after: float | None = None) -> str:
        logger.info("waiting Mirasim OTP -> %s (timeout %ss)", email_addr, timeout)
        issued_after = issued_after if issued_after is not None else time.time()
        grace_seconds = 180.0
        start = time.time()
        baseline_uid = 0

        conn0 = None
        try:
            conn0 = self._connect()
            conn0.select("INBOX")
            all_uids = self._search_uids(conn0, "ALL")
            if all_uids:
                baseline_uid = max(all_uids)
        except Exception as e:
            logger.debug("baseline uid init failed: %s", e)
        finally:
            if conn0 is not None:
                try:
                    conn0.logout()
                except Exception:
                    pass

        while time.time() - start < timeout:
            conn = None
            try:
                conn = self._connect()
                conn.select("INBOX")

                candidates: list[int] = []
                seen: set[int] = set()
                queries = (
                    f'(UNSEEN TO "{email_addr}")',
                    f'(TO "{email_addr}")',
                    '(UNSEEN FROM "mirasim")',
                    '(FROM "mirasim")',
                    '(UNSEEN FROM "mirofish")',
                    '(FROM "mirofish")',
                    "ALL",
                )
                for q in queries:
                    uids = self._search_uids(conn, q)
                    if q == "ALL" and uids:
                        uids = uids[-120:]
                    for uid in uids:
                        if uid in seen:
                            continue
                        seen.add(uid)
                        candidates.append(uid)

                for uid in sorted(set(candidates), reverse=True)[:80]:
                    if uid in self._consumed_uids:
                        continue
                    if baseline_uid and uid <= baseline_uid and (time.time() - start) < 20:
                        continue

                    status, msg_data = conn.uid("fetch", str(uid), "(INTERNALDATE BODY.PEEK[])")
                    if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    fetch_meta = msg_data[0][0]
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    from_val = self._decode_header_value(msg.get("From", "")).lower()
                    is_mira = "mirasim" in from_val or "mirofish" in from_val
                    recipient_matched = self._match_recipient(msg, email_addr)
                    if not recipient_matched and not is_mira:
                        continue
                    if self.catch_all_domain and not recipient_matched:
                        continue

                    subject = self._decode_header_value(msg.get("Subject", ""))
                    body = self._decode_payload(msg)
                    hint = f"{subject}\n{body}"
                    if not is_mira and not re.search(
                        r"(mirasim|mirofish|verification|one[\s-]?time|otp|验证码|code)",
                        hint,
                        flags=re.IGNORECASE,
                    ):
                        continue

                    otp = self._extract_otp(hint)
                    if not otp:
                        continue

                    email_digits = re.sub(r"\D", "", email_addr or "")
                    if email_digits and otp in email_digits:
                        continue

                    msg_ts = self._extract_internaldate_ts(fetch_meta) or self._message_timestamp(msg)
                    if msg_ts is not None and msg_ts + grace_seconds < issued_after:
                        if not (baseline_uid and uid > baseline_uid):
                            continue

                    self._consumed_uids.add(uid)
                    logger.info("got Mirasim OTP: %s", otp)
                    return otp
            except Exception as e:
                logger.warning("IMAP poll error: %s", e)
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            time.sleep(4)

        raise TimeoutError(f"wait Mirasim OTP timeout: {email_addr}")


def build_mail_provider(cfg: dict) -> MirasimMailProvider:
    return MirasimMailProvider(
        imap_server=str(cfg.get("imap_server") or "imap.qq.com"),
        imap_port=int(cfg.get("imap_port") or 993),
        email_addr=str(cfg.get("email") or "").strip(),
        auth_code=str(cfg.get("auth_code") or "").strip(),
        catch_all_domain=str(cfg.get("catch_all_domain") or "").strip(),
    )
