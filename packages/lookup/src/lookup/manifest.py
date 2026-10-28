"""Load and enforce the source manifest.

This module is where the inclusion principle is actually enforced, not merely
documented. Two guarantees:

1. `Source.files()` only ever yields paths under that source's declared root,
   matched by an explicit glob from `include`.
2. `Source.contains()` lets the pipeline re-check every document's path before
   it is written, so an extractor that wandered (a symlink, a `..`, a bug)
   is caught rather than silently indexed.

Both matter because the disk holding this archive also holds data that must
never be indexed.
"""
from __future__ import annotations

import functools
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from . import config
from .vfs import LOCAL, FileSystem


class ManifestError(RuntimeError):
    pass


# ── Visibility: whose material this source holds, as data ────────────────────
#
# A fact inherits the visibility of the system it came from. A company-wide
# Slack channel, an unrestricted Notion page, a GitLab group marked `internal`:
# whatever is read there can be reused freely. A DM, a private channel, a
# permission-restricted page: usable only after the fact is verified against
# its source and the user has signed it off. Nothing is ever banned outright —
# provenance decides the ceremony, not whether the material may be used at all.
#
# This lives in the manifest because it is a property of the SOURCE, not of a
# document: the manifest is already the single declaration of what may be
# indexed, so it is also the honest place to declare where that material came
# from. Search results carry the label through, so an agent reading a hit knows
# whether the fact is free to reuse or needs sign-off first.
VISIBILITY_OPEN = "open"
VISIBILITY_GATED = "gated"
# ⚠️ The default is UNSTATED, not "open", and that direction is deliberate. A
# source whose manifest block says nothing about visibility is of *unknown*
# provenance, and the provenance rule treats unknown as gated — so defaulting
# to "open" would silently assert the one thing that cannot be recovered from
# once something has been reused on the strength of it. Defaulting to "gated"
# would be safe but dishonest in the other direction: it would claim the
# manifest had made a judgement it never made, and there would then be no way
# to tell a deliberately-gated source from an undeclared one. So the default
# means "not stated", is reported as such, and is HANDLED as gated
# (`needs_signoff()` below) until someone declares otherwise.
VISIBILITY_UNSTATED = "unstated"
# ── `mixed`: the source declines to state one value, because its objects differ
#
# Added 2026-09-09. Slack is the case that forced it: a
# company-wide channel is `open` and a DM is `gated`, so ONE word for the whole
# corpus is wrong either way — `gated` mislabels the public majority, `open`
# mislabels the private minority, and leaving private channels out of the
# catalogue to dodge the problem violates the rule that nothing is banned as a
# source, only gated.
#
# The alternative was two sources, `slack-public` open and `slack-private`
# gated. It was named and rejected: cheap and honest today, and it breaks the
# moment one channel changes visibility, since the OBJECT would then have to
# move between sources. It also answers Slack only, so the next live system
# reopens the question.
#
# ⚠️ `mixed` is not a loosening. It says *ask the document*, and a document
# that has not answered is `unstated`, which is still handled as gated. What it
# removes is the dishonesty of one word that is wrong for most of the corpus.
VISIBILITY_MIXED = "mixed"
VISIBILITIES = (VISIBILITY_OPEN, VISIBILITY_GATED, VISIBILITY_UNSTATED,
                VISIBILITY_MIXED)

# The two values that are CEILINGS. A source declaring one of these has made a
# claim about its whole corpus, so a document inside it may narrow that claim
# and may never widen it: `open` plus a document saying `gated` is a document
# keeping itself back, which is always allowed; `gated` plus a document saying
# `open` is a document widening its own audience, which is the one operation
# that cannot be taken back and is therefore refused at ingest.
#
# `mixed` and `unstated` are NOT ceilings — neither has made a claim there is
# anything to widen past — so a document under them may say either, and saying
# nothing leaves it `unstated`.
VISIBILITY_CEILINGS = (VISIBILITY_OPEN, VISIBILITY_GATED)

# How much each value permits, for comparing a document against its source's
# ceiling. Only the ordering matters, and only between `open` and the rest:
# `gated` and `unstated` differ in what they RECORD, not in how they are
# handled, so they rank equally.
_PERMISSIVENESS = {VISIBILITY_OPEN: 1, VISIBILITY_GATED: 0,
                   VISIBILITY_UNSTATED: 0, VISIBILITY_MIXED: 0}


def widens(document: str, ceiling: str) -> bool:
    """True when a document claims a wider audience than its source allows.

    Only meaningful when `ceiling` is one of VISIBILITY_CEILINGS; a source that
    has stated no ceiling constrains nothing.
    """
    if ceiling not in VISIBILITY_CEILINGS:
        return False
    return _PERMISSIVENESS.get(document, 0) > _PERMISSIVENESS.get(ceiling, 0)


def effective_visibility(document: str | None, source: str) -> str:
    """A hit's label: the document's own value, else the source's.

    A `mixed` source with a document that said nothing resolves to `unstated`
    rather than to `mixed`. `mixed` describes a CORPUS and is not a statement
    about any one document, so reporting it on a hit would answer a question
    nobody asked — and it must still be HANDLED as gated, which `unstated`
    already is.
    """
    if document:
        return document
    return VISIBILITY_UNSTATED if source == VISIBILITY_MIXED else source


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    root: Path
    include: tuple[str, ...]
    extractor: str
    # ⚠️ MATCHED BY NAME, ANYWHERE UNDER THE ROOT — not by path. One entry can
    # therefore silently drop directories nobody meant to exclude, and it did:
    # the vault source excluded `Verda` so that `vault/Work/Verda` would not be
    # indexed twice alongside its own source, and the same entry also removed
    # `Job search/Applications/Example` — 19 interview
    # transcripts and job-search notes, which no other source declared. They
    # were in no source at all and nothing said so.
    #
    # This field is still the right tool for a name that means the same thing
    # wherever it appears — `.git`, `node_modules`, `.obsidian`. For one
    # particular subtree, use `exclude_paths`.
    exclude_names: tuple[str, ...] = field(default=())
    # Root-relative paths to prune, matched exactly against the walk's current
    # position rather than by basename. `Work/Verda` excludes that one subtree
    # and leaves every other directory called `Verda` alone.
    #
    # Deliberately not globs: an exclusion is a promise about what is NOT
    # indexed, and a glob is harder to read as such than a path. If a pattern
    # is ever genuinely needed, add it as a third field rather than making this
    # one ambiguous.
    exclude_paths: tuple[str, ...] = field(default=())
    # A named Docker volume that supplies this root inside the container,
    # instead of a bind mount from the host. Set it for a source on a network
    # share: Docker mounts cifs inside the VM, where a walk of the knowledge
    # archive costs 3 s against 34 s through a virtiofs bind mount of the
    # host's own SMB mount. `lookup mounts --write` emits the volume rather
    # than a bind for these, and the compose file defines it.
    # A network-backed source declares HOW it is mounted, here, rather than a
    # volume being named in the compose file and taught about per corpus.
    # Decision 2026-09-07: "make sure that lookup sources are also agnostic —
    # no archive/projects or other specific names, just generic sources: remote
    # or local, even better just sources."
    #
    #   [source.volume]
    #   type = "cifs"
    #   device = "//host/share"
    #   options = "ro,vers=3.0,nounix,noserverino"
    #   username = "${SHARE_USER}"      # a reference, never a value
    #   password = "${SHARE_PASS}"
    #
    # `lookup mounts --write` generates the compose `volumes:` definition from
    # this, so the base compose file contains no source-specific name at all,
    # and the ${...} references are emitted VERBATIM for compose to interpolate
    # from .env — a credential must never land in a generated file.
    volume: dict | None = None
    # Several addresses that name the SAME location. Decision 2026-09-07:
    # "check that lookup sources include aliases for the sources, as
    # smb://core/knowledge-archive/ is the same as
    # smb://192.168.1.90/knowledge-archive/."
    #
    # Purely declarative. Document identity is already address-independent --
    # sha1(source|path|key) with `path` relative to a root -- so listing an
    # alias changes nothing about what is indexed. What it buys is a check:
    # an address that is NOT listed is refused, so a typo in the environment
    # cannot quietly mount a different share under the same source name and
    # produce a second copy of every document.
    aliases: tuple[str, ...] = field(default=())
    # `open`, `gated`, `mixed` or `unstated` — see the block above `Source` for
    # what each means and why the default is `unstated` rather than `open`.
    #
    #   visibility = "open"    # a company-wide channel, an unrestricted page
    #   visibility = "gated"   # a DM, a private channel, a restricted page
    #   visibility = "mixed"   # the objects differ; each document declares its own
    #   (omitted)              # not stated; reported as such, handled as gated
    #
    # Validated in `load()`, so a typo is a loud manifest error rather than a
    # source that quietly reads as unstated.
    visibility: str = VISIBILITY_UNSTATED
    # The filesystem this source is read through. None means "decide from the manifest"
    # (today always the local disk); tests and other backends can supply one explicitly.
    filesystem: FileSystem | None = field(default=None, repr=False, compare=False)

    def needs_signoff(self) -> bool:
        """True when a fact from here is not free to reuse as it stands.

        Only a declared `open` is free. `gated` needs verification against the
        source plus user sign-off; `unstated` is treated the same way,
        because an undeclared source is of unknown provenance and unknown
        resolves to gated. The two stay distinguishable in the manifest and in
        the label — what they share is only how they must be handled.

        ⚠️ **A `mixed` source needs sign-off at the SOURCE level and that is not
        the whole answer** — its documents carry their own values, and an `open`
        document inside it is free to reuse. This method answers the question
        "what does the source say", which for `mixed` is "ask the document". Use
        `effective_visibility()` for a hit, never this.
        """
        return self.visibility != VISIBILITY_OPEN

    def ceiling(self) -> str | None:
        """The widest value a document here may claim, or None if unconstrained.

        `mixed` and `unstated` constrain nothing: neither has made a claim about
        the corpus, so there is nothing for a document to widen past.
        """
        return self.visibility if self.visibility in VISIBILITY_CEILINGS else None

    @property
    def fs(self) -> FileSystem:
        """Where this source's bytes come from. Every read of it goes through here."""
        if self.filesystem is not None:
            return self.filesystem
        from . import smb  # lazy: a local-only manifest never imports it
        try:
            return smb.for_source(self) or LOCAL
        except smb.SmbConfigError as e:
            raise ManifestError(str(e)) from None

    def path(self, s: str):
        """A path object for a logical path string, on this source's filesystem."""
        return self.fs.path(s)

    def exists(self) -> bool:
        return self.fs.is_dir(self.root)

    def addresses(self) -> tuple[str, ...]:
        """The declared aliases, normalised, deduped, order preserved."""
        out: list[str] = []
        for a in self.aliases:
            n = normalise_address(a)
            if n and n not in out:
                out.append(n)
        return tuple(out)

    def accepts_address(self, addr: str | None) -> bool:
        """True when `addr` names this source's location.

        No aliases declared means no claim either way, so anything is accepted:
        a source that has not been told its addresses cannot judge one.
        """
        known = self.addresses()
        if not known or not addr:
            return True
        return normalise_address(addr) in known

    def volume_name(self) -> str | None:
        """The Docker volume name, DERIVED from the source name.

        ⚠️ Renaming a source therefore orphans its volume: the old one is left
        behind and a new one is created. Nothing is lost when the volume is a
        network share — it holds no data, it *is* the share — but a `docker
        volume rm` is needed to tidy up.
        """
        if not self.volume:
            return None
        safe = re.sub(r"[^a-z0-9_.-]", "_", self.name.lower())
        return f"src_{safe}"

    def device(self) -> str | None:
        """The address this source is mounted from, normalised."""
        if not self.volume:
            return None
        dev = self.volume.get("device")
        return normalise_address(dev) if dev else None

    def contains(self, p) -> bool:
        """True only if `p` really resolves inside this source's root."""
        return self.fs.contains(self.root, p)

    def files(self):
        """Explicitly included files, deduped and ordered.

        Only the declared globs are matched, and a file matched by a glob but
        resolving outside the root (symlink escape) is dropped.

        ⚠️ **Excluded directories are PRUNED during the walk, not filtered
        afterwards.** `root.glob("**/*.md")` visits every directory under the
        root and only then discards the excluded ones, which is invisible on a
        local disk and brutal over a network share: walking the knowledge
        archive over SMB took **34 s for 3,610 files**, of which 3,080 were
        under an excluded `_sources/`. Pruning that subtree instead takes
        **3 s for the 530 files that were wanted** — an 11x difference for the
        same result, and the difference between a network source being usable
        or not.

        `full_match` rather than `PurePath.match`, because `match` does not
        treat `**` as spanning directories and the include patterns rely on it.
        (`PurePath.full_match` is Python 3.13+; this package supports 3.12, so
        the module carries its own.)
        """
        if not self.exists():
            return
        root = self.root
        fs = self.fs
        out: list = []
        # Normalised once: a declared `Work/Verda/` or `./Work/Verda` should
        # match the same subtree as `Work/Verda`.
        pruned_paths = {PurePath(p.strip("/")) for p in self.exclude_paths}
        for here, dirnames, filenames in fs.walk(root):
            # Prune in place: os.walk honours mutation of `dirnames`, which is
            # what stops it descending at all.
            #
            # Both exclusion kinds prune here. `exclude_names` matches a
            # basename wherever it occurs; `exclude_paths` matches one subtree
            # by its root-relative path, which is what an entry meaning "this
            # particular directory" needs — see the field's comment for the 19
            # documents the name form dropped by accident.
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in self.exclude_names
                and (here / d).relative_to(root) not in pruned_paths)
            for name in sorted(filenames):
                if name in self.exclude_names:
                    continue
                p = here / name
                rel = p.relative_to(root)
                if not any(full_match(rel, pat) for pat in self.include):
                    continue
                if not fs.is_file(p):
                    continue
                if not self.contains(p):
                    continue  # symlink or traversal out of the declared root
                out.append(p)
        for p in sorted(set(out)):
            yield p


@functools.cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a manifest glob to a regex over a POSIX-style relative path.

    Same rules as `PurePath.full_match` on POSIX, which this replaces for 3.12:
    the pattern must match the WHOLE path; `*` and `?` never cross a `/`; `**`
    as a whole segment spans zero or more directories; `[...]` is a character
    class and `[!...]` its negation. Matching is case-sensitive and dotfiles are
    not special, as they are not in pathlib.
    """
    segments = pattern.split("/")
    out: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        piece, j = [], 0
        while j < len(seg):
            c = seg[j]
            if c == "*":
                piece.append("[^/]*")
            elif c == "?":
                piece.append("[^/]")
            elif c == "[" and (end := seg.find("]", j + 2)) != -1:
                body = seg[j + 1:end]
                neg = body[:1] in ("!", "^")
                body = body[1:] if neg else body
                piece.append("[" + ("^" if neg else "") + body.replace("\\", "\\\\") + "]")
                j = end
            else:
                piece.append(re.escape(c))
            j += 1
        out.append("".join(piece) + ("" if last else "/"))
    return re.compile("".join(out), re.DOTALL)


def full_match(rel: PurePath, pattern: str) -> bool:
    return _glob_regex(pattern).fullmatch(rel.as_posix()) is not None


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(raw: str) -> Path:
    """Resolve ${VAR} in a root from the environment.

    ANY environment variable works, deliberately. A source is a source: there
    is no `PROJECTS`, no `ARCHIVE_PATH`, no per-corpus special case, and adding
    one is an edit to this manifest with no new variable and no code change.
    Decision 2026-09-07: *"lookup should just have 'sources', not projects or
    archive specifically, I should be able to add new source also."*

    `config.PLACEHOLDERS` still resolves the legacy `PI_*` names, because
    `sources.toml` (core's manifest, built on a different machine) uses them and
    is not this file's to rewrite. Environment variables win where both apply,
    since they are the mechanism a user actually reaches for.

    An unresolved variable is an error rather than an empty string: a root that
    silently becomes `/vault` would either index nothing or index the wrong
    tree, and the second is exactly what the inclusion principle exists to
    prevent.
    """
    def sub(m):
        name = m.group(1)
        val = os.environ.get(name)
        if val is None:
            legacy = config.PLACEHOLDERS.get(name)
            if legacy is None:
                raise ManifestError(
                    f"unresolved ${{{name}}} in root {raw!r} — set it in the "
                    f"environment (docker compose reads .env; see .env.example)")
            val = str(legacy)
        return val

    return Path(_VAR.sub(sub, raw))


def load(path: Path | None = None) -> dict[str, Source]:
    path = Path(path or config.MANIFEST)
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("source") or []
    if not entries:
        raise ManifestError(f"no [[source]] entries in {path}")

    out: dict[str, Source] = {}
    for e in entries:
        for required in ("name", "root", "include", "extractor"):
            if required not in e:
                raise ManifestError(f"source {e.get('name', '?')!r} missing {required!r}")
        name = e["name"]
        if name in out:
            raise ManifestError(f"duplicate source name: {name!r}")
        inc = e["include"]
        if isinstance(inc, str) or not inc:
            raise ManifestError(f"source {name!r}: include must be a non-empty list")
        vis = e.get("visibility", VISIBILITY_UNSTATED)
        if vis not in VISIBILITIES:
            raise ManifestError(
                f"source {name!r}: visibility must be one of "
                f"{', '.join(VISIBILITIES)} (got {vis!r}) — `open` for material "
                f"from a company-wide channel or an unrestricted page, `gated` "
                f"for a DM, a private channel or a permission-restricted page. "
                f"Omit the key to leave it unstated, which is reported as such "
                f"and handled as gated.")
        src = Source(
            name=name,
            description=" ".join((e.get("description") or "").split()),
            root=_expand(e["root"]),
            include=tuple(inc),
            extractor=e["extractor"],
            exclude_names=tuple(e.get("exclude_names") or ()),
            exclude_paths=tuple(e.get("exclude_paths") or ()),
            volume=_volume_of(e),
            aliases=tuple(e.get("aliases") or e.get("addresses") or ()),
            visibility=vis,
        )
        # A network source with a bad or unset credential reference fails HERE, loudly and
        # naming the variable, in the same way an unset ${VAR} in a root does. Nothing is
        # connected to; this only checks the declaration.
        src.fs
        out[name] = src
    return out


_ADDRESS = re.compile(r"^(?:smb:)?/{2}(?P<host>[^/\\]+)[/\\](?P<share>.+?)/?$")


def normalise_address(addr: str) -> str:
    """Reduce an SMB address to the `//host/share` form the cifs driver wants.

    `smb://core/knowledge-archive/`, `//core/knowledge-archive` and
    `\\\\core\\knowledge-archive` are one location written three ways, and the
    kernel accepts only the middle one. The host is lowercased because DNS is
    case-insensitive; the share is not, because SMB shares can be.

    Returns the input unchanged when it is not an SMB address, so a local path
    passes through untouched.
    """
    m = _ADDRESS.match(addr.strip().replace("\\", "/"))
    if not m:
        return addr.strip()
    return "//%s/%s" % (m.group("host").lower(), m.group("share").strip("/"))


def _volume_of(entry: dict) -> dict | None:
    """Read a `[source.volume]` table, tolerating the older string form.

    The string form named a volume defined in the compose file, which is the
    per-corpus special-casing this replaced. It is still accepted so an older
    manifest loads, but it carries no mount options — `mounts --write` says so
    rather than generating half a definition.
    """
    v = entry.get("volume")
    if v is None:
        return None
    if isinstance(v, str):
        return {"legacy_name": v}
    if not isinstance(v, dict):
        raise ManifestError(
            f"source {entry.get('name')!r}: `volume` must be a table, e.g. "
            f"[source.volume] with type/device/options")
    if not v.get("type") or not v.get("device"):
        raise ManifestError(
            f"source {entry.get('name')!r}: [source.volume] needs at least "
            f"`type` and `device`")
    return dict(v)
