"""Per-project image overlays (issue #113).

A project may commit a ``FROM``-less Dockerfile fragment under
``.vibepod/overlay/`` (shared) or ``.vibepod/overlay/<agent>/`` (per agent,
wins). The fragment is appended to the agent's base image and built into a
workspace-local image tagged content-addressed over the base image and the
build inputs, so unchanged overlays never rebuild and switching branches with
different overlays just switches images. The directory holding the Dockerfile
is the build context, so fragments can ``COPY`` files committed next to them.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.utils.console import info

OVERLAY_DIR = Path(".vibepod") / "overlay"
OVERLAY_HASH_LENGTH = 12
#: Image label carrying the workspace+agent identity; duplicated as a literal
#: in DockerManager.remove_stale_overlays to avoid an import cycle.
OVERLAY_KEY_LABEL = "vibepod.overlay.key"


def find_overlay_dockerfile(workspace: Path, agent: str) -> Path | None:
    """Return the overlay Dockerfile for *agent*, agent-specific one first.

    Symlinked Dockerfiles are ignored like every other symlink in the
    context: the target may live outside the committed overlay directory.
    """
    base = workspace / OVERLAY_DIR
    for candidate in (base / agent / "Dockerfile", base / "Dockerfile"):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _under_symlink(path: Path, context: Path) -> bool:
    """True when any directory between *context* (exclusive) and *path* is a symlink."""
    current = context
    for part in path.relative_to(context).parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _context_files(dockerfile: Path) -> list[Path]:
    """Context files hashed alongside the fragment, sorted by relative path.

    The Dockerfile itself is excluded (hashed separately as the fragment). For
    the shared overlay root, per-agent overlay roots are excluded too — they
    are separate build contexts with their own hashes. Symlinks (and anything
    reached through one) are excluded outright: hashing would read the target
    while the tar would ship the link, and a link can point outside the
    committed overlay directory.
    """
    context = dockerfile.parent
    skipped_roots = []
    for agent in SUPPORTED_AGENTS:
        agent_root = context / agent
        if (agent_root / "Dockerfile").is_file():
            skipped_roots.append(agent_root)
    files = []
    for path in context.rglob("*"):
        if path.is_symlink() or not path.is_file() or path == dockerfile:
            continue
        if any(root in path.parents for root in skipped_roots):
            continue
        if _under_symlink(path, context):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(context).as_posix())


def overlay_hash(base_ref: str, context_tar: bytes) -> str:
    """Content-address the build inputs: base image ref + build context tar.

    *base_ref* should be the base image's local id when it exists (so a moved
    ``latest`` tag re-hashes) and the tag string otherwise. *context_tar* is
    the byte-stable tar from :func:`build_context_tar`: its headers frame
    every member's path, mode, and content length, so distinct contexts can
    never collide, and hashing the same snapshot that gets built keeps the
    tag and the image content in lockstep even when files change mid-launch.
    """
    digest = hashlib.sha256()
    digest.update(base_ref.encode() + b"\n")
    digest.update(context_tar)
    return digest.hexdigest()[:OVERLAY_HASH_LENGTH]


def overlay_image_tag(agent: str, digest: str) -> str:
    return f"vibepod/overlay-{agent}:{digest}"


def _normalize_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Keep the tar byte-stable across owners and checkouts.

    Only the exec bit is build-relevant (and hashed by overlay_hash);
    uid/gid/mtime would otherwise vary per machine while the cache key does
    not, shipping contexts that differ from what was hashed.
    """
    member.mode = 0o755 if member.mode & 0o111 else 0o644
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    return member


def build_context_tar(base_image: str, dockerfile: Path) -> io.BytesIO:
    """Assemble the docker build context as an in-memory tar.

    Contains a synthesized ``Dockerfile`` (``FROM base_image`` prepended to
    the fragment, which therefore never needs a FROM line) plus the context
    files, so ``COPY`` works without ever writing into the user's overlay
    directory. The tar doubles as the snapshot fed to overlay_hash, so the
    context is read exactly once per launch.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        synthesized = f"FROM {base_image}\n".encode() + dockerfile.read_bytes()
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(synthesized)
        archive.addfile(info, io.BytesIO(synthesized))
        context = dockerfile.parent
        for path in _context_files(dockerfile):
            archive.add(
                path,
                arcname=path.relative_to(context).as_posix(),
                filter=_normalize_member,
            )
    buffer.seek(0)
    return buffer


def _overlay_key(workspace: Path, agent: str) -> str:
    """Stable identity of (workspace, agent) used to sweep superseded builds."""
    raw = f"{workspace.resolve().as_posix()}\n{agent}".encode()
    return hashlib.sha256(raw).hexdigest()[:OVERLAY_HASH_LENGTH]


def apply_overlay(
    manager: Any,
    workspace: Path,
    agent: str,
    base_image: str,
    *,
    rebuild: bool = False,
) -> str:
    """Return the image to run: the overlay image if the project has one.

    Builds (or reuses) the content-addressed overlay image on top of
    *base_image* and sweeps this workspace+agent's superseded overlay images
    after a successful build.
    """
    dockerfile = find_overlay_dockerfile(workspace, agent)
    if dockerfile is None:
        return base_image

    base_ref = manager.image_id(base_image) or base_image
    context_tar = build_context_tar(base_image, dockerfile)
    tag = overlay_image_tag(agent, overlay_hash(base_ref, context_tar.getvalue()))

    if rebuild or manager.image_id(tag) is None:
        info(f"Building overlay image {tag} from {base_image} ({dockerfile})")
        overlay_key = _overlay_key(workspace, agent)
        labels = {
            "vibepod.managed": "true",
            OVERLAY_KEY_LABEL: overlay_key,
            "vibepod.overlay.agent": agent,
        }
        # A forced rebuild must also bypass docker's layer cache, or RUN
        # commands like package updates would replay from cached layers.
        manager.build_image(context_tar, tag=tag, labels=labels, nocache=rebuild)
        manager.remove_stale_overlays(overlay_key, keep_tag=tag)
    else:
        info(f"Using cached overlay image {tag}")
    return tag
