"""Cursor Cloud Agents transport over the REST API.

Not the Python SDK, deliberately. The SDK is a client for a *locally
launched* ``cursor-agent`` bridge subprocess - it does not talk to
api.cursor.com directly - which would put a CLI binary inside every
container that spawns an agent, and its bridge launcher calls
``os.get_blocking`` and so cannot run on Windows at all.

The REST API has neither problem: stdlib HTTP, no binary, no platform
constraint, and it is already what the existing dispatch factory uses.

Implements the ``AgentTransport`` protocol in ``agentloop``.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .agentloop import AgentEvidence, SpawnRequest

API_BASE = "https://api.cursor.com/v1"
POLL_INTERVAL_S = 15
TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED",
            "finished", "error", "cancelled", "expired"}
_KEY_RE = re.compile(r'\s*(?:export\s+)?CURSOR_API_KEY\s*=\s*["\']?([^"\'\s]+)')


class CursorAPIError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{path} -> HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def is_usage_limit_error(exc: CursorAPIError) -> bool:
    """True when Cursor refused the call for account usage / hard limit."""
    body = (exc.body or "").lower()
    return (
        "usage_limit_exceeded" in body
        or "increase your hard limit" in body
        or "remaining until your hard limit" in body
    )


def load_api_key(search_from: Path | None = None) -> str:
    """Env first, then a .env in this repo or any sibling."""
    key = (os.getenv("CURSOR_API_KEY") or "").strip()
    if key:
        return key
    roots = [search_from] if search_from else []
    here = Path.cwd().resolve()
    roots += [here, *here.parents][:4]
    seen: set[Path] = set()
    for root in roots:
        if root is None:
            continue
        for candidate in [root / ".env", *(p / ".env" for p in root.iterdir()
                                           if p.is_dir())] if root.is_dir() else []:
            if candidate in seen or not candidate.exists():
                continue
            seen.add(candidate)
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                match = _KEY_RE.match(line)
                if match:
                    return match.group(1)
    raise CursorAPIError(0, "CURSOR_API_KEY not found in env or any .env", "auth")


def _git_target(doc: dict | None, *, _nested: bool = False
                ) -> tuple[str | None, str | None]:
    """Working branch and PR URL from a Cursor agent or run document.

    Cursor assigns ``target.branchName`` at create (``autoBranch: true``).
    The run payload's ``git.branches`` is often empty on ERROR. One
    extractor, pointed at whichever document we have.
    """
    if not isinstance(doc, dict):
        return None, None
    git = doc.get("git") if isinstance(doc.get("git"), dict) else {}
    branches = git.get("branches") or []
    branch = pr_url = None
    if branches and isinstance(branches[0], dict):
        branch = branches[0].get("branch") or None
        pr_url = branches[0].get("prUrl") or branches[0].get("pr_url") or None
    target = doc.get("target") if isinstance(doc.get("target"), dict) else {}
    branch = branch or target.get("branchName") or target.get("branch") or None
    pr_url = pr_url or target.get("prUrl") or target.get("url")
    if not _nested:
        inner = doc.get("agent") if isinstance(doc.get("agent"), dict) else None
        if inner is not None:
            nested_branch, nested_pr = _git_target(inner, _nested=True)
            branch = branch or nested_branch
            pr_url = pr_url or nested_pr
    return ((str(branch) if branch else None),
            (str(pr_url) if pr_url else None))


@dataclass
class RunHandle:
    agent_id: str
    run_id: str
    branch: str | None = None


class CursorRestTransport:
    """Spawn, poll and follow-up against the Cloud Agents API."""

    def __init__(self, api_key: str | None = None, base: str = API_BASE,
                 timeout: int = 60, trace: bool = False):
        self.key = api_key or load_api_key()
        self.base = base
        self.timeout = timeout
        self.trace = trace
        # agent_id → working branch assigned at create (or confirmed later).
        # Follow-up send() does not see the original handle, so we remember.
        self._working_branch: dict[str, str] = {}

    # ------------------------------------------------------------- http

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise CursorAPIError(exc.code,
                                 exc.read().decode("utf-8", "replace"), path)
        parsed = json.loads(raw) if raw.strip() else {}
        if self.trace:
            print(f"  [{method} {path}] -> {json.dumps(parsed)[:500]}")
        return parsed

    # -------------------------------------------------- AgentTransport

    def create(self, request: SpawnRequest) -> tuple[str, RunHandle]:
        # `repos: [{url, ref}]` confirmed against live agent records; a
        # request without a repo is accepted and then hangs in CREATING
        # forever, so the repo is effectively required.
        repo: dict = {"url": request.repo_url}
        if request.starting_ref:
            repo["startingRef"] = request.starting_ref
        # Cursor assigns the working branch at create (typically `cursor/...`,
        # autoBranch). We never pass a name in; we persist the one it returns,
        # same as startingRef. Confirm again from the agent document at finish.
        body: dict = {
            "prompt": {"text": request.prompt},
            "repos": [repo],
            "autoCreatePR": False,
        }
        if request.model:
            body["model"] = {"id": request.model}
        if request.env_vars:
            body["envVars"] = dict(request.env_vars)

        data = self._call("POST", "/agents", body)
        # The create response returns the durable agent and its first run,
        # and has been observed nesting both. Read every shape rather than
        # assuming one: losing the agent id here orphans a running agent
        # that is already costing money.
        agent = data.get("agent") if isinstance(data.get("agent"), dict) else data
        run = data.get("run") if isinstance(data.get("run"), dict) else {}
        agent_id = agent.get("id") or agent.get("agentId") or data.get("agentId") or ""
        run_id = (run.get("id") or agent.get("latestRunId")
                  or data.get("latestRunId") or data.get("runId") or "")
        if not agent_id:
            raise CursorAPIError(
                200, f"created an agent but could not find its id in the "
                     f"response: {json.dumps(data)[:400]}", "/agents")
        branch, _ = _git_target(agent)
        if not branch:
            branch, _ = _git_target(data)
        if not branch:
            branch, _ = _git_target(run)
        if branch:
            self._working_branch[agent_id] = branch
        return agent_id, RunHandle(agent_id, run_id, branch)

    def poll(self, agent_id: str, run_id: str) -> AgentEvidence:
        # Agent resources stay ACTIVE after a run finishes; polling
        # GET /agents/{id} alone never sees FINISHED (FP011, 2026-07-20).
        run_id = self._resolve_run_id(agent_id, run_id)
        data = self._call("GET", f"/agents/{agent_id}/runs/{run_id}")
        return self._to_evidence(agent_id, run_id, data)

    def wait(self, handle: RunHandle, max_wait_s: int = 3600,
             on_tick=None) -> AgentEvidence:
        deadline = time.monotonic() + max_wait_s
        while True:
            evidence = self.poll(handle.agent_id, handle.run_id)
            if on_tick:
                on_tick(evidence)
            if evidence.is_terminal:
                return self._enrich(evidence, known_branch=handle.branch)
            if time.monotonic() > deadline:
                evidence.status = "expired"
                return self._confirm_branch(evidence, handle.branch)
            time.sleep(POLL_INTERVAL_S)

    def send(self, agent_id: str, text: str) -> RunHandle:
        data = self._call("POST", f"/agents/{agent_id}/runs",
                          {"prompt": {"text": text}})
        run_id = self._extract_run_id(data)
        if not run_id:
            run_id = self._resolve_run_id(agent_id, "")
        if not run_id:
            raise CursorAPIError(
                200, f"follow-up accepted but no run id in response: "
                     f"{json.dumps(data)[:400]}", f"/agents/{agent_id}/runs")
        return RunHandle(agent_id, run_id,
                         self._working_branch.get(agent_id))

    def cancel(self, agent_id: str, run_id: str) -> None:
        self._call("POST", f"/agents/{agent_id}/runs/{run_id}/cancel", {})

    # ------------------------------------------------------------ detail

    def _extract_run_id(self, data: dict) -> str:
        """Read a run id from any response shape the API has returned."""
        run = data.get("run") if isinstance(data.get("run"), dict) else data
        agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        for candidate in (
            run.get("id"), run.get("runId"),
            agent.get("latestRunId"), agent.get("latest_run_id"),
            data.get("id"), data.get("runId"), data.get("latestRunId"),
        ):
            if candidate:
                return str(candidate)
        return ""

    def _resolve_run_id(self, agent_id: str, run_id: str) -> str:
        """Follow-up creates often omit the run id; the agent carries it."""
        if (run_id or "").strip():
            return run_id.strip()
        agent = self._call("GET", f"/agents/{agent_id}")
        return self._extract_run_id(agent)

    def _to_evidence(self, agent_id: str, run_id: str,
                     data: dict) -> AgentEvidence:
        status = str(data.get("status") or "").lower()
        branch, pr_url = _git_target(data)
        remembered = self._working_branch.get(agent_id)
        return AgentEvidence(
            agent_id=agent_id,
            run_id=run_id or str(data.get("id") or ""),
            status=status,
            result_text=str(data.get("result") or data.get("summary") or ""),
            branch=branch or remembered,
            pr_url=pr_url,
            usage=data.get("usage") or {},
            duration_ms=int(data.get("durationMs") or 0),
        )

    def _confirm_branch(self, evidence: AgentEvidence,
                        known_branch: str | None = None) -> AgentEvidence:
        """Fill the working branch from spawn state, then the agent document.

        GET /runs is polled for status (the agent resource stays ACTIVE).
        The UI's branch name lives on GET /agents/{id} as target.branchName.
        """
        if known_branch:
            evidence.branch = evidence.branch or known_branch
        remembered = self._working_branch.get(evidence.agent_id)
        if remembered:
            evidence.branch = evidence.branch or remembered
        try:
            agent = self._call("GET", f"/agents/{evidence.agent_id}")
            name, pr_url = _git_target(agent)
            if name:
                evidence.branch = evidence.branch or name
                self._working_branch[evidence.agent_id] = name
            if pr_url:
                evidence.pr_url = evidence.pr_url or pr_url
        except CursorAPIError:
            pass
        return evidence

    def _enrich(self, evidence: AgentEvidence,
                known_branch: str | None = None) -> AgentEvidence:
        """Branch identity, tool calls, stream errors, and usage once terminal."""
        self._confirm_branch(evidence, known_branch)
        try:
            calls, err = self._read_stream(evidence.agent_id, evidence.run_id)
            evidence.tool_calls = calls
            if err:
                evidence.error_code = err.get("code") or evidence.error_code
                evidence.error_message = (
                    err.get("message") or evidence.error_message)
                if not evidence.result_text and evidence.error_message:
                    evidence.result_text = evidence.error_message
        except CursorAPIError:
            evidence.tool_calls = []
        if not evidence.usage:
            try:
                usage = self._call(
                    "GET", f"/agents/{evidence.agent_id}/usage"
                            f"?runId={evidence.run_id}")
                evidence.usage = usage.get("totalUsage") or usage
            except CursorAPIError:
                pass
        return evidence

    def tool_calls(self, agent_id: str, run_id: str) -> list[dict]:
        calls, _ = self._read_stream(agent_id, run_id)
        return calls

    def _read_stream(self, agent_id: str, run_id: str
                     ) -> tuple[list[dict], dict | None]:
        """Replay the run SSE for tool calls and the real error payload.

        GET /runs often returns ``error: null`` on ERROR status. The stream
        carries ``type: error`` with ``{code, message}`` — without that the
        harness cannot tell why a PIT died in 20s with zero tools.
        """
        url = f"{self.base}/agents/{agent_id}/runs/{run_id}/stream"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.key}", "Accept": "text/event-stream",
        })
        calls: list[dict] = []
        stream_error: dict | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = str(event.get("type") or "")
                    if kind in ("tool-call-started", "tool-call-completed"):
                        payload = event.get("toolCall") or {}
                        args = payload.get("args") or {}
                        calls.append({
                            "call_id": event.get("callId"),
                            "name": payload.get("type") or payload.get("name"),
                            "phase": "started" if kind.endswith("started") else "completed",
                            "args": args,
                            "command": args.get("command"),
                            "result": payload.get("result"),
                        })
                    if kind == "error":
                        stream_error = {
                            "code": str(event.get("code")
                                        or (event.get("error") or {}).get("code")
                                        or ""),
                            "message": str(
                                event.get("message")
                                or (event.get("error") or {}).get("message")
                                or event.get("text")
                                or ""),
                        }
                    if kind in ("done", "result", "run-completed"):
                        break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return calls, stream_error
        return calls, stream_error
