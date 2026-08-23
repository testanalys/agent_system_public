"""Git diff introspection — the measurable shape of a change.

Everything here is computed over the *session diff*: the net difference
between the merge base and HEAD. That choice matters. An agent that adds
a line in one commit and removes it in the next has changed nothing, and
this module sees nothing — so an agent is free to iterate as much as it
likes inside a session without inflating its own footprint.
"""
from __future__ import annotations

import ast
import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BRANCH_TOKENS = re.compile(r"^\s*(if|elif|for|while|try|except|with|case|match)\b")
ENV_READ = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[(]|getenv\s*\()\s*["']([A-Z0-9_]{3,})["']""")
DEP_FILES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "package.json", "Pipfile",
}
DEP_LINE = re.compile(r"^\+\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]{1,})[\"']?\s*(?:[<>=!~^\[]|$)")
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".sh"}

# The harness writes its own bookkeeping into the working tree as an agent
# works, and that bookkeeping must never count as part of the agent's change.
# Without this a second submit measures the first submit's attempt log.
EXCLUDED_PATHS = ("agent_state/", ".agent_tmp/")
TEST_HINT = re.compile(r"(^|/)(tests?|test_[^/]+|[^/]+_test)\.|(^|/)tests?/")


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class FileChange:
    path: str
    status: str            # A(dded) M(odified) D(eleted) R(enamed)
    added: int = 0
    deleted: int = 0
    old_path: str | None = None

    @property
    def is_new(self) -> bool:
        return self.status.startswith("A")

    @property
    def is_delete(self) -> bool:
        return self.status.startswith("D")

    @property
    def is_code(self) -> bool:
        return Path(self.path).suffix in CODE_SUFFIXES

    @property
    def is_test(self) -> bool:
        return bool(TEST_HINT.search(self.path))


@dataclass
class SessionDiff:
    """The net change a session produced, plus everything derived from it."""
    repo: Path
    base: str
    head: str
    files: list[FileChange] = field(default_factory=list)
    patch: str = ""

    # ---------------------------------------------------------------- load

    @classmethod
    def collect(cls, repo: Path, base: str, head: str = "HEAD") -> "SessionDiff":
        merge_base = _git(repo, "merge-base", base, head).strip() or base
        scope = [f":(exclude){p}" for p in EXCLUDED_PATHS]
        numstat = _git(repo, "diff", "--numstat", "-M", merge_base, head, "--", *scope)
        namestat = _git(repo, "diff", "--name-status", "-M", merge_base, head,
                        "--", *scope)

        statuses: dict[str, tuple[str, str | None]] = {}
        for line in namestat.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                statuses[parts[1].replace("\\", "/")] = (parts[0], None)
            elif len(parts) == 3:                       # rename / copy
                statuses[parts[2].replace("\\", "/")] = (parts[0], parts[1])

        files: list[FileChange] = []
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, deleted, path = parts[0], parts[1], parts[-1].replace("\\", "/")
            status, old = statuses.get(path, ("M", None))
            files.append(FileChange(
                path=path,
                status=status,
                added=0 if added == "-" else int(added),
                deleted=0 if deleted == "-" else int(deleted),
                old_path=old,
            ))

        patch = _git(repo, "diff", "-M", merge_base, head, "--", *scope)
        return cls(repo=repo, base=merge_base, head=head, files=files, patch=patch)

    # ----------------------------------------------------------- primitives

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def files_touched(self) -> int:
        return len(self.files)

    @property
    def added_lines(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def deleted_lines(self) -> int:
        return sum(f.deleted for f in self.files)

    @property
    def net_loc(self) -> int:
        return self.added_lines - self.deleted_lines

    @property
    def gross_churn(self) -> int:
        return self.added_lines + self.deleted_lines

    @property
    def new_files(self) -> list[FileChange]:
        return [f for f in self.files if f.is_new]

    @property
    def touches_tests(self) -> bool:
        return any(f.is_test for f in self.files)

    @property
    def touches_non_test_code(self) -> bool:
        return any(f.is_code and not f.is_test for f in self.files)

    def _added_lines_iter(self):
        for line in self.patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                yield line[1:]

    def _removed_lines_iter(self):
        for line in self.patch.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                yield line[1:]

    # ------------------------------------------------------------- signals

    def new_dependencies(self) -> list[str]:
        """Packages added to a dependency manifest."""
        touched = [f for f in self.files if Path(f.path).name in DEP_FILES]
        if not touched:
            return []
        found: set[str] = set()
        in_dep_file = False
        for line in self.patch.splitlines():
            if line.startswith("+++ "):
                in_dep_file = Path(line[6:].strip()).name in DEP_FILES
                continue
            if not in_dep_file or not line.startswith("+") or line.startswith("+++"):
                continue
            match = DEP_LINE.match(line)
            if match and match.group(1) not in {"version", "name", "description"}:
                found.add(match.group(1))
        return sorted(found)

    def new_config_keys(self) -> list[str]:
        """Env vars read in added lines that were not read in removed ones."""
        after = {m for line in self._added_lines_iter() for m in ENV_READ.findall(line)}
        before = {m for line in self._removed_lines_iter() for m in ENV_READ.findall(line)}
        return sorted(after - before)

    def branch_tokens(self) -> int:
        """Net new control-flow statements."""
        added = sum(1 for line in self._added_lines_iter() if BRANCH_TOKENS.match(line))
        removed = sum(1 for line in self._removed_lines_iter() if BRANCH_TOKENS.match(line))
        return max(0, added - removed)

    def nesting_depth_delta(self) -> int:
        """Deepest added indentation vs deepest removed, in indent levels."""
        def depth(lines) -> int:
            best = 0
            for line in lines:
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip(" "))
                best = max(best, indent // 4)
            return best
        return max(0, depth(self._added_lines_iter()) - depth(self._removed_lines_iter()))

    def signature_changes(self, min_callers: int = 2) -> list[dict]:
        """Python functions whose signature changed and that have callers.

        Best-effort by design: a file that will not parse is skipped rather
        than failing the classification. A missed signature change costs a
        review that did not happen; a crash costs the whole submit.
        """
        out: list[dict] = []
        for change in self.files:
            if not change.path.endswith(".py") or change.is_new or change.is_delete:
                continue
            try:
                before = _git(self.repo, "show", f"{self.base}:{change.path}")
                after = (self.repo / change.path).read_text(encoding="utf-8")
            except (GitError, OSError, UnicodeDecodeError):
                continue
            old_sigs, new_sigs = _signatures(before), _signatures(after)
            for name, sig in old_sigs.items():
                if name in new_sigs and new_sigs[name] != sig:
                    callers = self._count_callers(name, exclude=change.path)
                    if callers > min_callers:
                        out.append({"symbol": name, "file": change.path,
                                    "callers": callers})
        return out

    def _count_callers(self, symbol: str, exclude: str = "") -> int:
        short = symbol.rsplit(".", 1)[-1]
        try:
            hits = _git(self.repo, "grep", "-l", "-F", f"{short}(", "--", "*.py")
        except GitError:
            return 0
        return len({h for h in hits.splitlines() if h and h != exclude})

    def crowded_directories(self, population_limit: int) -> list[dict]:
        """New files landing in directories that are already full."""
        out: list[dict] = []
        for change in self.new_files:
            parent = self.repo / Path(change.path).parent
            if not parent.is_dir():
                continue
            siblings = [p for p in parent.iterdir() if p.is_file()]
            if len(siblings) >= population_limit:
                out.append({"file": change.path, "directory": str(Path(change.path).parent),
                            "population": len(siblings)})
        return out

    def near_duplicate_files(self, threshold: float) -> list[dict]:
        """New files whose name closely resembles an existing neighbour."""
        out: list[dict] = []
        for change in self.new_files:
            target = Path(change.path)
            parent = self.repo / target.parent
            if not parent.is_dir():
                continue
            for sibling in parent.iterdir():
                if not sibling.is_file() or sibling.name == target.name:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, target.stem.lower(), sibling.stem.lower()
                ).ratio()
                if ratio >= threshold:
                    out.append({"file": change.path, "resembles": sibling.name,
                                "similarity": round(ratio, 2)})
                    break
        return out

    def deleted_tests(self) -> list[str]:
        return [f.path for f in self.files if f.is_test and f.is_delete]

    def added_skips(self) -> list[str]:
        """Test skips/xfails introduced by this change."""
        hits: list[str] = []
        current = ""
        for line in self.patch.splitlines():
            if line.startswith("+++ "):
                current = line[6:].strip()
            elif line.startswith("+") and re.search(r"\b(skip|xfail|it\.skip|describe\.skip)\b", line):
                if TEST_HINT.search(current):
                    hits.append(current)
        return sorted(set(hits))


def _signatures(source: str) -> dict[str, str]:
    """Map of qualified function name -> rendered argument list."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}

    def visit(node, prefix: str = "") -> None:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[f"{prefix}{child.name}"] = ast.dump(child.args)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree)
    return out
