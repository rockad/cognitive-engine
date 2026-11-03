"""Out-of-tree experience storage with a mandatory human sign-off (RFC 004 §2.6).

Lessons are written as PENDING candidates and become `EXPERIENCE.md` / `INDEX.md` entries
only through `approve`, which needs a named human. No MCP tool calls `approve`; it is a
CLI step (`cognitive-harness2 experience approve <id> --by NAME`), so an agent can stage a
lesson but can never commit one.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class SignoffRequired(PermissionError):
    """A lesson cannot be committed without a named human approver."""


class ExperienceError(ValueError):
    pass


@dataclass(frozen=True)
class Pending:
    id: str
    domain: str
    lesson: str


class ExperienceStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _domain_dir(self, domain: str) -> Path:
        if not _SLUG.match(domain):
            raise ExperienceError(f"domain {domain!r} must match {_SLUG.pattern}")
        d = (self.root / domain).resolve()
        if self.root.resolve() not in d.parents:
            raise ExperienceError(f"domain {domain!r} resolves outside the experience root")
        return d

    def _pending_dir(self) -> Path:
        return self.root / ".pending"

    def stage(self, domain: str, lesson: str) -> Pending:
        self._domain_dir(domain)
        if not lesson.strip():
            raise ExperienceError("a lesson cannot be empty")
        pid = secrets.token_hex(6)
        self._pending_dir().mkdir(parents=True, exist_ok=True)
        (self._pending_dir() / f"{pid}.json").write_text(
            json.dumps({"id": pid, "domain": domain, "lesson": lesson.strip()}), encoding="utf-8")
        return Pending(pid, domain, lesson.strip())

    def pending(self) -> list[Pending]:
        d = self._pending_dir()
        if not d.is_dir():
            return []
        return [Pending(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(d.glob("*.json"))]

    def approve(self, pending_id: str, *, approved_by: str | None) -> Path:
        if not approved_by or not approved_by.strip():
            raise SignoffRequired("naming the approving human is required to commit an experience")
        if not re.fullmatch(r"[0-9a-f]{12}", pending_id):
            raise ExperienceError(f"{pending_id!r} is not a pending id")
        f = self._pending_dir() / f"{pending_id}.json"
        if not f.is_file():
            raise ExperienceError(f"no pending lesson {pending_id}")
        item = json.loads(f.read_text(encoding="utf-8"))
        d = self._domain_dir(item["domain"])
        d.mkdir(parents=True, exist_ok=True)
        with (d / "EXPERIENCE.md").open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {pending_id}\n\n{item['lesson']}\n\n_Approved by {approved_by.strip()}._\n")
        with (d / "INDEX.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {pending_id}: {item['lesson'].splitlines()[0][:100]}\n")
        f.unlink()
        return d / "EXPERIENCE.md"
