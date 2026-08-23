"""Record that a production-branch push should become the running job.

The agent container does not run gcloud. Pushing to the production branch
(`master` for discovery, `live` for pipeline) is the handoff. A Cloud
Build trigger watching that branch builds the image tagged with the git
SHA and updates the Cloud Run job spec to that digest.

See `cloud/realize-on-push.yaml` for the trigger shape.
"""
from __future__ import annotations

from pathlib import Path

BUILDS = {
    "pipeline": ("pipeline/cloud/cloudbuild-pipeline.yaml", "your-gcp-project"),
    "discovery": ("cloudbuild-mechanical.yaml", "your-gcp-project"),
}


def rebuild(subsystem: str, repo: Path, *, dry_run: bool = False) -> dict:
    """Note the expected Cloud Build path. Never submits a build from here."""
    spec = BUILDS.get(subsystem)
    return {
        "ok": True,
        "path": "production_branch_push",
        "dry_run": dry_run,
        "config": spec[0] if spec else None,
        "project": spec[1] if spec else None,
        "note": (
            "The production-branch push is the rebuild signal. Cloud Build "
            "must tag the image with the git SHA and update the Cloud Run "
            "job to that digest. This process does not run gcloud."
        ),
    }
