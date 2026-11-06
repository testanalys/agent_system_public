"""Load a Secret Manager value into os.environ via ADC (no service-account file).

Same pattern as the pipeline operator scripts: Cloud Run may bind the secret
directly; local and the dashboard service both fall back to Secret Manager
when the env var is unset. Do not set GOOGLE_APPLICATION_CREDENTIALS to a
long-lived key for this.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


def ensure_env_from_secret(
    env_name: str,
    *,
    secret_id: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    """Return env_name's value, fetching it from Secret Manager if unset."""
    existing = (os.getenv(env_name) or "").strip()
    if existing:
        return existing

    secret_id = secret_id or env_name
    project = (
        project
        or (os.getenv("GCP_SECRETS_PROJECT") or "").strip()
        or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        or "regest-ai"
    )

    import google.auth
    import google.auth.transport.requests

    creds, detected = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if not project and detected:
        project = detected
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    token = creds.token
    if not token:
        raise RuntimeError("failed to obtain GCP access token from ADC")

    url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project}/"
        f"secrets/{secret_id}/versions/latest:access"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode())
    import base64

    raw = payload.get("payload", {}).get("data") or ""
    val = base64.b64decode(raw).decode("utf-8").strip() if isinstance(raw, str) else ""
    if not val:
        raise RuntimeError(f"Secret Manager {secret_id} returned an empty payload")
    os.environ[env_name] = val
    return val
