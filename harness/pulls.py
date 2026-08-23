"""Open a GitHub pull request from an agent branch into the production branch.

The token is the same GH_TOKEN the job already uses to push. A missing
token is recorded, not a crash: the operator card in GCS still exists.

Operator PRs opened by the harness always start with ``PR_TITLE_PREFIX``
so automations can filter them without parsing the rest of the title:

    [turnstile-operator] discovery — headline here
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Stable keyword at the start of every harness-opened operator PR.
# Automations: match titles that start with this string (exact).
PR_TITLE_PREFIX = "[turnstile-operator]"


def token() -> str:
    return (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()


def operator_title(subsystem: str, headline: str) -> str:
    """Canonical PR title for AWAITING_OPERATOR dispositions."""
    head = (headline or "").strip() or "review required"
    return f"{PR_TITLE_PREFIX} {subsystem} — {head}"[:256]


def open_pr(repo_slug: str, head: str, base: str, title: str,
            body: str = "") -> dict:
    """POST /repos/{slug}/pulls. Never raises into the decision path."""
    auth = token()
    if not auth:
        return {"ok": False, "reason": "GH_TOKEN is not set"}
    if not repo_slug or not head:
        return {"ok": False, "reason": "repo or head branch missing"}

    payload = json.dumps({
        "title": title[:256],
        "head": head,
        "base": base,
        "body": body[:60000],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo_slug}/pulls",
        data=payload,
        headers={
            "Authorization": f"Bearer {auth}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "turnstile",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "url": data.get("html_url") or "",
                "number": data.get("number"),
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        # Already-open PR is success enough: parse the existing URL if present.
        if exc.code == 422 and "already exists" in detail.lower():
            return {"ok": True, "status": 422, "reason": "already open",
                    "detail": detail}
        return {"ok": False, "reason": f"HTTP {exc.code}", "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
