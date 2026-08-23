"""Roles: what an agent is, and how its prompt is assembled.

There is no import-time registry any more. Which agents exist is a
property of the subsystem, so it is read from the repo the agent is
about to work on:

    from roles import load
    registry = load(repo, repo_url=url, subsystem="pipeline")
    role = registry.get("pipeline-funnel-leak")
    prompt = render(role, context, registry.orientation,
                    getattr(registry, "scale_framework", "") or None)

Nothing under `agents/` is imported. It is parsed as data, by a parser
that can only produce strings, because those files are written by agents
and this package runs in the process that judges them.
"""
from __future__ import annotations

from .base import Registry, Role, RoleContext, render
from .definition import (MASTER, PIT, RETRO, AgentDefinition,
                         DefinitionError)
from .loader import load

__all__ = ["load", "Registry", "Role", "RoleContext", "render",
           "AgentDefinition", "DefinitionError",
           "PIT", "RETRO", "MASTER"]
