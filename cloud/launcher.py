"""Start one Cloud Run execution per agent.

An agent is a unit of work with a beginning and an end, and Cloud Run
already runs those concurrently, in isolation, with its own retry and its
own logs. So one agent is one execution. Twenty concurrent agents is
twenty executions of the same job, and one of them failing is one
execution failing.

This is what the previous shape gave up by putting every agent behind a
single scheduled tick: three subsystems shared one container, so a git
clone failing in discovery aborted a pipeline merge, and "how many agents
may run at once" became a question the code had to answer instead of a
number the platform already enforces.

Liveness is read from `executions.list` and the arguments each execution
was started with - the same principle the data plane's reconciler uses.
There is no in-flight file, because a file is a claim about the world and
the execution list is the world.
"""
from __future__ import annotations

import json
import os

DEFAULT_JOB = "turnstile-agent"
DEFAULT_CONTAINER = "turnstile-agent"


def job_name() -> str:
    return (os.getenv("TURNSTILE_AGENT_JOB") or "").strip() or DEFAULT_JOB


def container_name() -> str:
    return (os.getenv("TURNSTILE_AGENT_CONTAINER") or "").strip() or DEFAULT_CONTAINER


def _project() -> str:
    return ((os.getenv("TURNSTILE_CLOUD_RUN_PROJECT") or "").strip()
            or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
            or "your-gcp-project")


def _region() -> str:
    return ((os.getenv("TURNSTILE_CLOUD_RUN_REGION") or "").strip()
            or (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
            or "us-central1")


def _job_path(job: str) -> str:
    return f"projects/{_project()}/locations/{_region()}/jobs/{job}"


def agent_args(role: str, subsystem: str, trigger: dict,
               context: dict | None = None) -> list[str]:
    """The command line one agent execution runs."""
    args = ["-m", "cloud.agent_job",
            "--role", role,
            "--subsystem", subsystem,
            "--trigger", json.dumps(trigger, default=str)]
    if context:
        args += ["--context", json.dumps(context, default=str)]
    return args


def master_args(subsystem: str) -> list[str]:
    return ["-m", "cloud.agent_job", "--subsystem", subsystem, "--master"]


def launch(args: list[str], *, job: str | None = None) -> dict:
    """Start one execution. Returns what happened; never raises."""
    target = job or job_name()
    try:
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        request = run_v2.RunJobRequest(
            name=_job_path(target),
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        name=container_name(), args=args)
                ]
            ),
        )
        operation = client.run_job(request=request)
        execution = _execution_name(operation, client, target)
        if not execution:
            return {"launched": False, "job": target,
                    "reason": "RunJob accepted but no execution name returned "
                               "(check IAM: roles/run.developer + run.invoker "
                               "on the target job for this service account)"}
        return {"launched": True, "job": target, "execution": execution}
    except Exception as exc:                          # noqa: BLE001
        return {"launched": False, "job": target,
                "reason": f"{type(exc).__name__}: {exc}"}


def _execution_name(operation, client, job: str) -> str | None:
    """Resolve the Cloud Run execution id from a RunJob LRO."""
    op = getattr(operation, "operation", None)
    if op is not None:
        name = getattr(op, "name", None) or ""
        # v2 RunJob returns an operations/* LRO; poll briefly for Execution.
        if name and "/operations/" in name:
            try:
                op.result(timeout=120)
            except Exception:                          # noqa: BLE001
                pass
        response = getattr(op, "response", None)
        if response is not None:
            for attr in ("name", "execution", "execution_name"):
                value = getattr(response, attr, None)
                if value:
                    return str(value).rsplit("/", 1)[-1]
    # Fallback: newest execution for this job (best-effort).
    try:
        from google.cloud import run_v2 as run_v2_mod

        exec_client = run_v2_mod.ExecutionsClient()
        for execution in exec_client.list_executions(parent=_job_path(job)):
            if getattr(execution, "completion_time", None):
                continue
            return execution.name.rsplit("/", 1)[-1]
    except Exception:                                  # noqa: BLE001
        pass
    raw = getattr(op, "name", None) if op is not None else None
    return str(raw).rsplit("/", 1)[-1] if raw else None


def in_flight(match: list[str], *, job: str | None = None) -> list[str]:
    """Executions still running whose arguments contain every token in `match`.

    Used for exactly one question - is a master already thinking about
    this subsystem - and answered by the platform rather than by us. PIT
    and retro agents deliberately do not ask it: two agents looking at
    the same breach is cheap, and a guard that suppresses the second one
    is how a stuck first one silences a whole tier.
    """
    target = job or job_name()
    try:
        from google.cloud import run_v2

        client = run_v2.ExecutionsClient()
        out: list[str] = []
        for execution in client.list_executions(parent=_job_path(target)):
            if getattr(execution, "completion_time", None):
                continue
            args = _args_of(execution)
            if all(token in args for token in match):
                out.append(execution.name.rsplit("/", 1)[-1])
        return out
    except Exception:                                 # noqa: BLE001
        return []


def _args_of(execution) -> list[str]:
    template = getattr(execution, "template", None)
    containers = getattr(template, "containers", None) or []
    args: list[str] = []
    for container in containers:
        args.extend(list(getattr(container, "args", None) or []))
    return args
