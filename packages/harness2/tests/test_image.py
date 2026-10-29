"""The proposer image reference: pinned or refused (pure checks), then the real image on the real daemon."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cognitive_harness2 import image
from cognitive_harness2.sandbox import VerifiedDocker

V = VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock")
BASE = "ubuntu:26.04@sha256:" + "a" * 64
HASH12 = "0123456789ab"


def sources(tmp_path: Path, dockerfile: str = f"FROM {BASE}\nRUN true\n", proxy: str = "# proxy\n"):
    d = tmp_path / "c"
    d.mkdir(exist_ok=True)
    (d / "Dockerfile").write_text(dockerfile)
    p = tmp_path / "egress_proxy.py"
    p.write_text(proxy)
    return d, p


@pytest.mark.parametrize("ref", [
    "sha256:" + "0" * 64, f"{image.REPO}:{HASH12}", BASE, "alpine:3@sha256:" + "b" * 64,
    "registry.example/org/img:1.2@sha256:" + "c" * 64,
])
def test_pinned_references_pass(ref):
    assert image.validate_reference(ref) == ref


@pytest.mark.parametrize("ref", [
    "", " ", None, 5, "ubuntu", "ubuntu:26.04", "ubuntu:latest", "ubuntu:latest@sha256:" + "a" * 64,
    f"{image.REPO}", f"{image.REPO}:latest", f"{image.REPO}:{HASH12}x", f"{image.REPO}:ABCDEFABCDEF", "sha256:abc",
    "sha256:" + "G" * 64, "ubuntu@sha256:" + "a" * 64 + " ", "ubuntu:26.04@sha256:short", "-v /:/h:latest",
    "evil/" + image.REPO + f":{HASH12}",
])
def test_unpinned_or_untagged_references_are_refused(ref):
    with pytest.raises(image.ImageError):
        image.validate_reference(ref)


def test_the_image_ref_only_holds_pinned_values():
    assert image.ImageRef(f"{image.REPO}:{HASH12}", "sha256:" + "1" * 64).tag.endswith(HASH12)
    for tag, i in ((f"{image.REPO}:latest", "sha256:" + "1" * 64), (f"{image.REPO}:{HASH12}", "latest"),
                   ("ubuntu:26.04", "sha256:" + "1" * 64), (f"{image.REPO}:{HASH12}", "")):
        with pytest.raises(image.ImageError):
            image.ImageRef(tag, i)


@pytest.mark.parametrize("text", [
    "FROM ubuntu:26.04\n", "FROM ubuntu\n", "FROM ubuntu:latest@sha256:" + "a" * 64 + "\nFROM ubuntu:26.04\n",
    "FROM ubuntu:26.04@sha256:abc\n", "RUN true\n", f"FROM {BASE}\nFROM {BASE}\n", "FROM scratch\n",
])
def test_an_unpinned_or_multi_stage_base_is_refused(text):
    with pytest.raises(image.ImageError):
        image.base_reference(text)


def test_the_committed_dockerfile_is_pinned_and_minimal():
    text = (image.CONTAINER_DIR / "Dockerfile").read_text()
    assert image.base_reference(text).startswith("ubuntu:26.04@sha256:")
    assert "USER 65534" in text and "--no-install-recommends" in text and "/var/lib/apt/lists" in text
    assert "latest" not in text.replace("latest.", "")


def test_the_tag_names_its_sources(tmp_path):
    d, p = sources(tmp_path)
    tag1, full1, base = image.expected(d, p)
    assert tag1 == f"{image.REPO}:{full1[:12]}" and base == BASE
    p.write_text("# proxy changed\n")
    assert image.expected(d, p)[0] != tag1
    (d / "Dockerfile").write_text(f"FROM {BASE}\nRUN false\n")
    assert image.expected(d, p)[0] != tag1
    # field boundaries matter: moving a byte from one source to the other changes the hash
    assert image.content_hash(b"ab", b"c") != image.content_hash(b"a", b"bc")


def test_missing_sources_and_an_unpinned_dockerfile_fail_closed(tmp_path):
    with pytest.raises(image.ImageError, match="cannot read"):
        image.expected(tmp_path / "nope", tmp_path / "nope.py")
    d, p = sources(tmp_path, dockerfile="FROM ubuntu:26.04\n")
    with pytest.raises(image.ImageError, match="not pinned"):
        image.expected(d, p)


def fake(calls, *, rc=0, out="", err=""):
    def run(argv, **kw):
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, rc, out, err)
    return run


def test_resolve_checks_tag_and_label_with_a_pinned_docker_and_no_docker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://evil:1")
    d, p = sources(tmp_path)
    tag, full, _ = image.expected(d, p)
    calls: list = []
    ref = image.resolve_image(V, run=fake(calls, out=f"sha256:{'9' * 64}|{full}\n"), container_dir=d, proxy_source=p)
    assert ref == image.ImageRef(tag, "sha256:" + "9" * 64)
    (argv, kw), = calls
    assert argv[:4] == [V.path, "-H", V.host, "image"] and argv[-1] == tag and "pull" not in argv and "build" not in argv
    assert not any(k.upper().startswith("DOCKER_") for k in kw["env"])


def test_resolve_refuses_a_missing_image_and_a_label_mismatch_and_never_builds(tmp_path):
    d, p = sources(tmp_path)
    calls: list = []
    with pytest.raises(image.ImageError, match="not present locally"):
        image.resolve_image(V, run=fake(calls, rc=1, err="No such image"), container_dir=d, proxy_source=p)
    with pytest.raises(image.ImageError, match="content label"):
        image.resolve_image(V, run=fake(calls, out=f"sha256:{'9' * 64}|{'f' * 64}\n"), container_dir=d, proxy_source=p)
    with pytest.raises(image.ImageError, match="content label"):
        image.resolve_image(V, run=fake(calls, out=f"sha256:{'9' * 64}|\n"), container_dir=d, proxy_source=p)
    with pytest.raises(image.ImageError):
        image.resolve_image(V, run=fake(calls, out=f"latest|{image.expected(d, p)[1]}\n"), container_dir=d, proxy_source=p)
    assert all(argv[3:5] == ["image", "inspect"] for argv, _ in calls)


def test_build_uses_a_minimal_context_labels_and_the_pinned_socket(tmp_path):
    d, p = sources(tmp_path)
    tag, full, _ = image.expected(d, p)
    calls: list = []
    seen_ctx: dict = {}

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[3] == "build":
            ctx = Path(argv[-1])
            seen_ctx["files"] = sorted(x.name for x in ctx.iterdir())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, f"sha256:{'7' * 64}|{full}\n", "")
    ref = image.build(V, run=run, container_dir=d, proxy_source=p)
    assert ref.tag == tag and seen_ctx["files"] == ["Dockerfile", "egress_proxy.py"]
    build = calls[0]
    assert build[:4] == [V.path, "-H", V.host, "build"]
    assert f"{image.LABEL_CONTENT}={full}" in build and not Path(build[-1]).exists()


def test_a_failed_build_raises_with_the_tail_of_the_error(tmp_path):
    d, p = sources(tmp_path)
    with pytest.raises(image.ImageError, match="boom"):
        image.build(V, run=fake([], rc=1, err="x\nboom"), container_dir=d, proxy_source=p)


# ── the real image on the real daemon ────────────────────────────────────────────

@pytest.mark.requires_rootless_docker
def test_the_real_image_is_pinned_small_and_minimal(verified, proposer_image):
    out = subprocess.run([verified.path, "-H", verified.host, "image", "inspect", proposer_image.id, "--format",
                          '{{.Config.User}}|{{index .Config.Labels "' + image.LABEL_BASE + '"}}|{{.Size}}'],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    user, base, size = out.split("|")
    assert user == "65534:65534" and base.startswith("ubuntu:26.04@sha256:") and int(size) < 300 * 1024 * 1024
    probe = subprocess.run([verified.path, "-H", verified.host, "run", "--rm", "--pull", "never", "--network", "none",
                            proposer_image.id, "sh", "-c",
                            "ls /var/lib/apt/lists | wc -l; test -x /bin/sh && echo sh; test -e /opt/harness2/egress_proxy.py "
                            "&& echo proxy; command -v apt-get curl wget sudo nc | wc -l; id -u"],
                           capture_output=True, text=True, timeout=60).stdout.split()
    assert probe[0] == "0" and probe[1:3] == ["sh", "proxy"] and probe[4] == "65534"
    assert int(probe[3]) == 1          # apt-get only: no curl, wget, sudo or nc in the image
