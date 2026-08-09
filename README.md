# Mirasim Reg

Unofficial local panel for Mirasim / Mirofish email signup, invite redeem, and invite-code chaining.

Uses QQ IMAP + a catch-all domain mailbox to receive the 6-digit login code, then talks to the public auth HTTP APIs.

> Not affiliated with Mirasim / Mirofish / Shanda.  
> Protocol endpoints were observed from the desktop client. Use at your own risk and only with mailboxes / invites you are allowed to use.

## Features

- Generate catch-all alias emails and poll QQ IMAP for Mirasim OTP
- `POST /auth/code` → verify → optional `/auth/invite/redeem`
- Create / fetch referral invite via `/auth/referral`
- Batch register with invite reuse (default 10 uses per code) then queue the next code
- Simple web UI + SSE live logs
- Accounts saved locally to `data/accounts.json` (gitignored)

## Quick start

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy config.example.json config.json   # Windows
# cp config.example.json config.json  # macOS / Linux
```

Edit `config.json`:

```json
{
  "login_base": "https://admin.test.mirofish.ai",
  "imap_server": "imap.qq.com",
  "imap_port": 993,
  "email": "your_qq@qq.com",
  "auth_code": "your_qq_imap_auth_code",
  "catch_all_domain": "mail.example.com",
  "seed_invite": "",
  "otp_timeout": 120,
  "invite_uses_per_code": 10
}
```

Run:

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8788
```

Open [http://127.0.0.1:8788/](http://127.0.0.1:8788/)

Windows shortcut: `run.bat`

## Auth flow

1. Create alias: `random@your-catch-all-domain`
2. `POST {login_base}/auth/code` with `{"email":"..."}`
3. Read 6-digit code from QQ IMAP inbox
4. `POST {login_base}/auth/verify` with email + code → `access_token` / `refresh_token`
5. Optional: `POST /auth/invite/redeem` with Bearer token + invite code
6. `POST /auth/referral` to create / ensure an invite for the next account

Default `login_base` for the currently observed desktop build:

- `https://admin.test.mirofish.ai`

There is also a production-looking host in the client:

- `https://admin.mirofish.ai`

Switch it in the UI if needed.

## Mail setup

1. Enable IMAP in QQ Mail and create an authorization code
2. Point a catch-all (or wildcard) domain mailbox / forwarder to that QQ inbox
3. Put QQ address + auth code + catch-all domain into `config.json`

## Project layout

```
mirasim_api.py   # auth HTTP client
mail_otp.py      # QQ IMAP + OTP extraction
register.py      # single / batch registration + invite chain
server.py        # FastAPI + SSE panel
index.html       # frontend
config.example.json
```

## Security notes

- Never commit `config.json` or `data/accounts.json`
- Tokens in `data/accounts.json` are equivalent to account login
- This tool binds to `127.0.0.1` by default

## Disclaimer

This project is for research / personal automation learning.  
Respect Mirasim / Mirofish terms of service and local laws. The authors are not responsible for misuse.

## License

MIT
