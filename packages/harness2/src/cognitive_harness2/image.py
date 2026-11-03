"""The proposer session image: which one, whether it is here, and how to build it (RFC 004 6.2.3).

The image is never pulled or built implicitly. A session starts only from an image that

  * was built from `containers/proposer/Dockerfile` whose `FROM` is pinned by DIGEST,
  * carries the tag `cognitive-harness2-proposer:<12 hex of sha256(Dockerfile, proxy source)>`, so the tag
    names exactly the source that produced it (edit the Dockerfile or the proxy and the tag changes),
  * carries a label with the full content hash, which must agree with the tag, and
  * is then run by its IMAGE ID, so the tag cannot be re-pointed between the check and the run.

`resolve_image` checks all of that against the pinned daemon and returns an `ImageRef`; anything else
(`latest`, no tag, an untagged name, a base without a digest, a label that disagrees) raises `ImageError`.
`build` is the one call that creates the image, and only `python -m cognitive_harness2.image build` (or
`containers/proposer/build.sh`) makes it. Every docker call is pinned to the verified socket with `-H` and
runs without any DOCKER_* variable.

    uv run python -m cognitive_harness2.image status
    uv run python -m cognitive_harness2.image build
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import sandbox

REPO = "cognitive-harness2-proposer"
LABEL_CONTENT = "org.cognitive-engine.proposer.content"
LABEL_BASE = "org.cognitive-engine.proposer.base"
PACKAGE_DIR = Path(__file__).resolve().parents[2]
CONTAINER_DIR = PACKAGE_DIR / "containers" / "proposer"
PROXY_SOURCE = Path(__file__).resolve().with_name("egress_proxy.py")
PROXY_PATH_IN_IMAGE = "/opt/harness2/egress_proxy.py"

_BASE_PINNED = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_OUR_TAG = re.compile(rf"^{re.escape(REPO)}:[0-9a-f]{{12}}$")
_BUILD_TIMEOUT_S = 900


class ImageError(RuntimeError):
    """The proposer image is missing, unpinned, or not the one this source builds. Fail closed."""


@dataclass(frozen=True)
class ImageRef:
    tag: str        # cognitive-harness2-proposer:<hash12>
    id: str         # sha256:<64 hex>: what `docker run` is given

    def __post_init__(self) -> None:
        if not _OUR_TAG.fullmatch(self.tag) or not _IMAGE_ID.fullmatch(self.id):
            raise ImageError(f"not a pinned proposer image reference: tag={self.tag!r} id={self.id!r}")


def validate_reference(ref: object) -> str:
    """Accept only a reference that is pinned: an image ID, a `name:tag@sha256:digest`, or our content tag.

    `latest`, a missing tag and a bare name are refused: they name whatever the daemon holds today.
    """
    if not isinstance(ref, str) or not ref or ref != ref.strip():
        raise ImageError("the image reference must be a non-empty string")
    if _IMAGE_ID.fullmatch(ref) or _OUR_TAG.fullmatch(ref) or _BASE_PINNED.fullmatch(ref):
        if ref.split(":", 1)[1].split("@", 1)[0] == "latest":
            raise ImageError("the image reference uses the mutable tag 'latest'")
        return ref
    raise ImageError(f"the image reference {ref[:80]!r} is not pinned (need an image id, name:tag@sha256:digest "
                     f"or {REPO}:<content hash>)")


def base_reference(dockerfile: str) -> str:
    """The single `FROM` of the Dockerfile, which must be pinned by digest."""
    froms = [m for m in re.findall(r"^\s*FROM\s+(\S+)", dockerfile, flags=re.IGNORECASE | re.MULTILINE)]
    if len(froms) != 1:
        raise ImageError(f"the Dockerfile must have exactly one FROM (found {len(froms)})")
    if not _BASE_PINNED.fullmatch(froms[0]):
        raise ImageError(f"the base image {froms[0]!r} is not pinned by digest (name:tag@sha256:...)")
    return froms[0]


def read_sources(container_dir: Path = CONTAINER_DIR, proxy_source: Path = PROXY_SOURCE) -> tuple[bytes, bytes]:
    try:
        return (container_dir / "Dockerfile").read_bytes(), proxy_source.read_bytes()
    except OSError as e:
        raise ImageError(f"cannot read the image sources ({e.filename}): {e.strerror}") from None


def content_hash(dockerfile: bytes, proxy: bytes) -> str:
    h = hashlib.sha256()
    for part in (b"dockerfile", dockerfile, b"proxy", proxy):
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return h.hexdigest()


def expected(container_dir: Path = CONTAINER_DIR, proxy_source: Path = PROXY_SOURCE) -> tuple[str, str, str]:
    """(tag, full content hash, pinned base) for the current sources. Refuses an unpinned base."""
    dockerfile, proxy = read_sources(container_dir, proxy_source)
    base = base_reference(dockerfile.decode("utf-8"))
    full = content_hash(dockerfile, proxy)
    return f"{REPO}:{full[:12]}", full, base


def _run(verified: sandbox.VerifiedDocker, args: list[str], run: Callable[..., subprocess.CompletedProcess],
         timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=timeout,
                   env=sandbox._pinned_env())
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ImageError(f"docker {args[0]} failed: {type(e).__name__}") from None


def resolve_image(verified: sandbox.VerifiedDocker, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                  container_dir: Path = CONTAINER_DIR, proxy_source: Path = PROXY_SOURCE) -> ImageRef:
    """The local image for the current sources, checked, or `ImageError`. Never builds and never pulls."""
    tag, full, _ = expected(container_dir, proxy_source)
    fmt = '{{.Id}}|{{index .Config.Labels "' + LABEL_CONTENT + '"}}'
    p = _run(verified, ["image", "inspect", "--format", fmt, tag], run)
    if p.returncode != 0:
        raise ImageError(f"the proposer image {tag} is not present locally and is never built or pulled at run "
                         "time; build it once with: uv run python -m cognitive_harness2.image build")
    image_id, _, label = p.stdout.strip().partition("|")
    if label != full:
        raise ImageError(f"the image {tag} does not carry the content label of the current sources; rebuild it")
    return ImageRef(tag, image_id)


def build(verified: sandbox.VerifiedDocker, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
          container_dir: Path = CONTAINER_DIR, proxy_source: Path = PROXY_SOURCE) -> ImageRef:
    """Build the image from a minimal temporary context (the Dockerfile and the proxy source, nothing else)."""
    tag, full, base = expected(container_dir, proxy_source)
    dockerfile, proxy = read_sources(container_dir, proxy_source)
    ctx = Path(tempfile.mkdtemp(prefix="h2-image-ctx-"))
    try:
        (ctx / "Dockerfile").write_bytes(dockerfile)
        (ctx / "egress_proxy.py").write_bytes(proxy)
        p = _run(verified, ["build", "--tag", tag, "--label", f"{LABEL_CONTENT}={full}",
                            "--label", f"{LABEL_BASE}={base}", "--label", f"org.opencontainers.image.base.name={base}",
                            "--label", "org.opencontainers.image.title=cognitive-harness2 proposer session image",
                            str(ctx)], run, timeout=_BUILD_TIMEOUT_S)
    finally:
        shutil.rmtree(ctx, ignore_errors=True)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-8:])
        raise ImageError(f"docker build failed:\n{tail}")
    return resolve_image(verified, run=run, container_dir=container_dir, proxy_source=proxy_source)


def gate_for_build() -> sandbox.VerifiedDocker:
    """The rootless gate, checked against the (pinned) base image the Dockerfile builds from."""
    _, _, base = expected()
    return sandbox.verify_rootless_docker(image=base)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("status", "build"):
        print("usage: python -m cognitive_harness2.image status|build", file=sys.stderr)
        return 2
    try:
        tag, full, base = expected()
        print(f"sources: tag {tag}\n         base {base}")
        verified = gate_for_build()
        ref = build(verified) if args[0] == "build" else resolve_image(verified)
    except (ImageError, sandbox.SandboxUnavailable) as e:
        print(f"image: {e}", file=sys.stderr)
        return 1
    print(f"image:   {ref.tag}\nid:      {ref.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
