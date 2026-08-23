"""Objects addressed by key, backed by GCS or a directory.

Two callers, and neither of them is a queue: the proposal ledger, which
is the only state this layer keeps, and the run archive, which nothing
reads to make a decision.

The previous design put a work queue here - inbox, claimed, processed,
generation preconditions for a compare-and-swap - to stop two ticks
spawning the same agent twice. That race only existed because one
scheduled process was the sole spawner. Agents are now spawned at the
point the problem is detected, by the process that detected it, so there
is nothing to hand over and nothing to race for.

An unconfigured store is a normal state, not an error. `put` returns
None, `list_keys` returns nothing, and callers carry on. The one thing
that genuinely needs durability - a raised escalation - says so at its
own call site.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def uri() -> str:
    return (os.getenv("TURNSTILE_AGENT_STORE") or "").strip()


def configured() -> bool:
    return bool(uri())


def _bucket_name() -> str | None:
    value = uri()
    return value[5:].split("/", 1)[0] if value.startswith("gs://") else None


def _local_root() -> Path | None:
    value = uri()
    return Path(value).expanduser() if value and not value.startswith("gs://") else None


def _bucket():
    name = _bucket_name()
    if not name:
        return None
    from google.cloud import storage

    return storage.Client().bucket(name)


def put(key: str, payload: dict | str) -> str | None:
    """Write one object. Returns its address, or None if there is no store."""
    body = (payload if isinstance(payload, str)
            else json.dumps(payload, indent=2, default=str) + "\n")

    root = _local_root()
    if root is not None:
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return str(path)

    bucket = _bucket()
    if bucket is None:
        return None
    try:
        bucket.blob(key).upload_from_string(body, content_type="application/json")
        return f"gs://{bucket.name}/{key}"
    except Exception as exc:                              # noqa: BLE001
        import sys

        print(f"[objectstore] put failed for {key}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return None


def get(key: str) -> str | None:
    root = _local_root()
    if root is not None:
        path = root / key
        return path.read_text(encoding="utf-8") if path.exists() else None

    bucket = _bucket()
    if bucket is None:
        return None
    blob = bucket.blob(key)
    try:
        return blob.download_as_text()
    except Exception:                                  # noqa: BLE001 - absent
        return None


def get_json(key: str) -> dict | None:
    raw = get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def list_keys(prefix: str) -> list[str]:
    root = _local_root()
    if root is not None:
        base = root / prefix
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(root)).replace("\\", "/")
                      for p in base.rglob("*") if p.is_file())

    bucket = _bucket()
    if bucket is None:
        return []
    return sorted(blob.name for blob in bucket.client.list_blobs(
        bucket, prefix=prefix))


def delete(key: str) -> None:
    root = _local_root()
    if root is not None:
        (root / key).unlink(missing_ok=True)
        return

    bucket = _bucket()
    if bucket is None:
        return
    try:
        bucket.blob(key).delete()
    except Exception:                                  # noqa: BLE001 - absent
        pass


def move(source: str, target: str) -> None:
    body = get(source)
    if body is None:
        return
    put(target, body)
    delete(source)
