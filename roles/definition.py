"""An agent definition, as data that cannot execute.

Definitions live in the repo an agent investigates, under ``agents/``, and
a master agent may write them. That single fact decides the format.

**Nothing here is ever imported.** If a definition were Python, the
operating system would ``import`` agent-authored code into the process
that classifies agents, merges their branches and holds the deploy
credentials - and it would do so at spawn time, before any classifier
sees the diff. Markdown has no execution semantics, so the worst a bad
definition can do is be a bad prompt, which the oracle lints and the
classifier tiers.

The parser is deliberately small and deliberately strict. It accepts
scalars and comma lists and rejects every other line with its number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PIT, RETRO, MASTER = "pit", "retro", "master"
TIERS = (PIT, RETRO, MASTER)

REQUIRED = ("name", "description", "tier")
LIST_FIELDS = ("metrics", "capture")
# `repo` and `model` are deliberately absent. Which repository an agent is
# spawned against and which model it runs on are the operating system's
# decisions; a definition that could name its own repo would let an agent
# point the next spawn somewhere it was never granted.
KNOWN = ("name", "description", "tier", "subsystem", "signature",
         *LIST_FIELDS)

_DELIM = "---"
_LINE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)\s*:\s*(?P<value>.*)$")
# {dims.batch} or {dims.batch|this batch}
_PLACEHOLDER = re.compile(r"\{dims\.(?P<key>[a-z0-9_]+)(?:\|(?P<default>[^}]*))?\}")


class DefinitionError(ValueError):
    """A definition that cannot be trusted to spawn an agent."""


@dataclass(frozen=True)
class AgentDefinition:
    """One agent, parsed. Identity and prompt body, nothing executable."""
    name: str
    description: str
    tier: str
    subsystem: str = ""
    signature: str | None = None
    metrics: tuple[str, ...] = ()
    capture: tuple[str, ...] = ()
    body: str = ""
    source: Path | None = field(default=None, compare=False)

    @property
    def id(self) -> str:
        return self.name


# ══════════════════════════════════════════════════════════════════ parsing

def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body). Raises when the block is missing or unclosed."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != _DELIM:
        raise DefinitionError(
            "no frontmatter. An agent definition opens with a '---' line, "
            "then 'name:', 'description:' and 'tier:', then a closing "
            "'---', then the prompt body.")
    for index in range(1, len(lines)):
        if lines[index].strip() == _DELIM:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:]).strip()
    raise DefinitionError("frontmatter is opened with '---' but never closed")


def parse_frontmatter(block: str) -> dict:
    """Scalars and comma lists. Anything else is an error with its line."""
    data: dict = {}
    for number, raw in enumerate(block.split("\n"), start=2):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            raise DefinitionError(
                f"line {number}: expected 'key: value', got {line!r}. "
                f"Frontmatter holds plain keys and values only.")
        key, value = match.group("key"), match.group("value").strip()
        if key not in KNOWN:
            raise DefinitionError(
                f"line {number}: unknown field {key!r}. Known fields: "
                + ", ".join(KNOWN))
        if key in data:
            raise DefinitionError(f"line {number}: {key!r} is set twice")
        data[key] = value
    return data


def parse(text: str, source: Path | None = None) -> AgentDefinition:
    block, body = split_frontmatter(text)
    data = parse_frontmatter(block)

    missing = [f for f in REQUIRED if not data.get(f)]
    if missing:
        raise DefinitionError("frontmatter is missing: " + ", ".join(missing))

    tier = data["tier"].lower()
    if tier not in TIERS:
        raise DefinitionError(
            f"tier {tier!r} is not one of: " + ", ".join(TIERS))
    if not body.strip():
        raise DefinitionError(
            "the prompt body is empty. The body is what the agent reads; "
            "the frontmatter only says who it is.")

    def _list(key: str) -> tuple[str, ...]:
        raw = data.get(key, "")
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    return AgentDefinition(
        name=data["name"].strip(),
        description=data["description"].strip(),
        tier=tier,
        subsystem=data.get("subsystem", "").strip(),
        signature=(data.get("signature") or "").strip() or None,
        metrics=_list("metrics"),
        capture=_list("capture") or _list("metrics"),
        body=body,
        source=source,
    )


def load_file(path: Path) -> AgentDefinition:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DefinitionError(f"cannot read {path}: {exc}") from exc
    try:
        definition = parse(text, source=Path(path))
    except DefinitionError as exc:
        raise DefinitionError(f"{Path(path).name}: {exc}") from exc
    stem = Path(path).stem
    if definition.name != stem:
        raise DefinitionError(
            f"{Path(path).name}: declares name {definition.name!r} but the "
            f"filename says {stem!r}. They must match - the filename is what "
            f"decides whether an agent is editing its own definition.")
    return definition


# ═════════════════════════════════════════════════════════════════ rendering

def fill(body: str, dims: dict | None) -> str:
    """Substitute ``{dims.key|default}`` placeholders."""
    values = dims or {}

    def _replace(match: re.Match) -> str:
        key = match.group("key")
        default = match.group("default")
        value = values.get(key)
        if value in (None, ""):
            return default if default is not None else key.replace("_", " ")
        return str(value)

    return _PLACEHOLDER.sub(_replace, body)


# ═══════════════════════════════════════════════════════════════ orientation

def parse_orientation(text: str) -> dict[str, str]:
    """``_orientation.md`` -> {metric_id: how to read it}."""
    out: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith("## "):
            if current:
                out[current] = " ".join(buffer).strip()
            current = raw[3:].strip()
            buffer = []
        elif current:
            buffer.append(raw.strip())
    if current:
        out[current] = " ".join(buffer).strip()
    return {k: v for k, v in out.items() if v}
