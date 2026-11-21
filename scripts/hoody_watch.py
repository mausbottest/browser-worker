#!/usr/bin/env python3
"""Poll Hoody's free-tier status and claim the free server as soon as capacity appears.

Runs from GitHub Actions every 15 minutes. Writes status/hoody.json (committed back
to the repo) so the chat side can watch it with a plain URL check.
"""
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = "https://api.hoody.com"
EMAIL = os.environ["HOODY_EMAIL"]
PASSWORD = os.environ["HOODY_PASSWORD"]
STATUS_DIR = pathlib.Path("status")
STATUS_FILE = STATUS_DIR / "hoody.json"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(method, path, token=None, body=None, base=API):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "hoody-watch/1.0")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}


def main():
    state = {
        "checked_at": now(),
        "ok": False,
        "step": "login",
        "claimed": False,
        "note": "",
    }

    # 1. Fresh token each run (login tokens last 24h).
    code, res = call("POST", "/api/v1/users/auth/login",
                     body={"email": EMAIL, "password": PASSWORD})
    token = ((res or {}).get("data") or {}).get("token")
    if code != 200 or not token:
        state.update(step="login", note=f"login HTTP {code}: {json.dumps(res)[:300]}")
        write(state)
        return 1
    state["ok"] = True

    # 2. Where do we stand?
    code, res = call("GET", "/api/v1/users/me/free-tier-status", token=token)
    ft = (res or {}).get("data") or {}
    state["free_tier"] = ft
    state["step"] = "status"

    code, res = call("GET", "/api/v1/computers?page=1", token=token)
    computers = ((res or {}).get("data") or {}).get("computers") or []
    state["computers"] = [
        {k: c.get(k) for k in ("id", "name", "status", "server_name", "created_at")}
        for c in computers
    ]

    if computers or ft.get("has_free_server"):
        state.update(claimed=True, note="free server already exists")
        write(state)
        return 0

    reason = ft.get("claim_blocked_reason")
    if reason in ("pool_empty", "invite_required", "support_required"):
        state["note"] = f"waiting: {reason}"
        write(state)
        return 0

    # 3. Capacity looks open — nudge setup, then poll for up to ~90s.
    state["step"] = "retry-setup"
    code, res = call("POST", "/api/v1/users/me/retry-setup", token=token)
    state["retry_setup"] = {"http": code, "body": res}
    if code == 403 and "FREE_TIER_INVITE_REQUIRED" in json.dumps(res):
        state["note"] = "invite_required"
        write(state)
        return 0
    if code == 200 and ((res or {}).get("data") or {}).get("blocked_reason") == "support_required":
        state["note"] = "support_required"
        write(state)
        return 0

    state["step"] = "poll"
    for _ in range(6):
        time.sleep(15)
        code, res = call("GET", "/api/v1/computers?page=1", token=token)
        computers = ((res or {}).get("data") or {}).get("computers") or []
        if computers:
            state["computers"] = [
                {k: c.get(k) for k in ("id", "name", "status", "server_name", "created_at")}
                for c in computers
            ]
            state.update(claimed=True, note="free server provisioned")
            write(state)
            return 0

    code, res = call("GET", "/api/v1/users/me/free-tier-status", token=token)
    state["free_tier_after"] = (res or {}).get("data") or {}
    state["note"] = "setup requested, no computer yet"
    write(state)
    return 0


def write(state):
    STATUS_DIR.mkdir(exist_ok=True)
    STATUS_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Hoody watch {state['checked_at']}\n\n")
            f.write(f"- claimed: **{state['claimed']}**\n")
            f.write(f"- note: {state.get('note')}\n")
            if state.get("free_tier"):
                f.write(f"- free tier: `{json.dumps(state['free_tier'])}`\n")
            f.write(f"- computers: {len(state.get('computers') or [])}\n")


if __name__ == "__main__":
    sys.exit(main())
