"""Per-project image overlays (issue #113).

A project may commit a ``FROM``-less Dockerfile fragment under
``.vibepod/overlay/`` (shared) or ``.vibepod/overlay/<agent>/`` (per agent,
wins). The fragment is appended to the agent's base image and built into a
workspace-local image tagged content-addressed over the base image and the
build inputs, so unchanged overlays never rebuild and switching branches with
different overlays just switches images. The directory holding the Dockerfile
is the build context, so fragments can ``COPY`` files committed next to them.

Every project gets its own image repository — ``overlay-<agent>-<project>``
(issue #145) — so a machine running several projects through the same agent
shows one readable row per project in ``docker images`` instead of a stack of
identically named ones.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.utils.console import info

OVERLAY_DIR = Path(".vibepod") / "overlay"
OVERLAY_HASH_LENGTH = 12
#: Image label carrying the workspace+agent identity; duplicated as a literal
#: in DockerManager.remove_stale_overlays to avoid an import cycle.
OVERLAY_KEY_LABEL = "vibepod.overlay.key"
#: Longest project slug embedded in an image name; keeps `docker images` rows
#: readable when a project directory has a very long name.
SLUG_MAX_LENGTH = 24
#: Slug used when a workspace has no usable directory name (e.g. ``/``).
SLUG_FALLBACK = "workspace"


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


def _slugify(name: str) -> str:
    """Reduce *name* to a single repository path component.

    Such a component must match ``[a-z0-9]+([._-][a-z0-9]+)*``, so runs of
    anything else collapse to a single dash and the edges are trimmed (after
    truncation, which can itself expose a trailing dash). A name that carries
    no usable character at all falls back to :data:`SLUG_FALLBACK`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
    slug = slug[:SLUG_MAX_LENGTH].strip("-")
    return slug or SLUG_FALLBACK


def project_slug(workspace: Path) -> str:
    """Docker-safe, human-recognizable name for *workspace*.

    The workspace directory name is the part a user recognizes, so that — not
    the full path, which would leak the host layout into image names — is what
    goes into the image repository.
    """
    return _slugify(workspace.resolve().name)


def overlay_hash(base_ref: str, context_tar: bytes, overlay_key: str) -> str:
    """Content-address the build inputs: base image ref + context tar + key.

    *base_ref* should be the base image's local id when it exists (so a moved
    ``latest`` tag re-hashes) and the tag string otherwise. *context_tar* is
    the byte-stable tar from :func:`build_context_tar`: its headers frame
    every member's path, mode, and content length, so distinct contexts can
    never collide, and hashing the same snapshot that gets built keeps the
    tag and the image content in lockstep even when files change mid-launch.
    *overlay_key* makes the digest per-workspace: two projects with byte-identical
    overlays would otherwise land on one image, of which only the first
    builder's ``OVERLAY_KEY_LABEL`` is recorded — so the other project's
    sweep would delete an image still in use elsewhere.
    """
    digest = hashlib.sha256()
    digest.update(base_ref.encode() + b"\n")
    digest.update(overlay_key.encode() + b"\n")
    digest.update(context_tar)
    return digest.hexdigest()[:OVERLAY_HASH_LENGTH]


def overlay_repository(agent: str, slug: str) -> str:
    """Fully qualified repository (the tag without its digest).

    *slug* names the project (see :func:`project_slug`) so that every project
    owns a distinct repository and ``docker images`` stays readable when
    several projects run the same agent.

    The explicit ``localhost/`` registry keeps the name literal under Podman,
    which otherwise qualifies the unqualified name to ``docker.io/...`` at
    build time but resolves it against ``localhost/`` on later lookups — so
    the cached image is never found again (and the sweep in
    ``remove_stale_overlays`` would even delete the build it should keep).
    Docker treats the name as a plain literal either way.
    """
    return f"localhost/vibepod/overlay-{agent}-{slug}"


def overlay_image_tag(agent: str, slug: str, digest: str) -> str:
    """Fully qualified local tag for the overlay image."""
    return f"{overlay_repository(agent, slug)}:{digest}"


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


@dataclass(frozen=True)
class ResolvedOverlay:
    """The overlay image a workspace+agent resolves to, built or not yet."""

    tag: str
    #: The tag without its digest — the part that does not move with the base
    #: image, and the only part a caller can trust when *base_local* is False.
    repository: str
    dockerfile: Path
    key: str
    #: Build context snapshot the tag was hashed over, ready to hand to docker.
    context_tar: io.BytesIO
    #: True when the tag already exists locally, i.e. a launch would reuse it.
    built: bool
    #: True when the base image was present locally, so its id — not the bare
    #: tag string — went into the digest. False means *tag* is provisional:
    #: ``vp run`` pulls the base first and would hash a different ref.
    base_local: bool


def resolve_overlay_image(
    manager: Any,
    workspace: Path,
    agent: str,
    base_image: str,
) -> ResolvedOverlay | None:
    """Resolve the overlay image for *workspace*+*agent* without building it.

    Returns None when the project has no overlay for *agent*. Reading the
    context is the only cost, so callers that merely report the image (``vp
    list``) get the answer a launch would compute — as long as the base image
    is already local, which ``base_local`` tells them.
    """
    dockerfile = find_overlay_dockerfile(workspace, agent)
    if dockerfile is None:
        return None

    base_id = manager.image_id(base_image)
    key = _overlay_key(workspace, agent)
    context_tar = build_context_tar(base_image, dockerfile)
    repository = overlay_repository(agent, project_slug(workspace))
    digest = overlay_hash(base_id or base_image, context_tar.getvalue(), key)
    tag = f"{repository}:{digest}"
    return ResolvedOverlay(
        tag=tag,
        repository=repository,
        dockerfile=dockerfile,
        key=key,
        context_tar=context_tar,
        built=manager.image_id(tag) is not None,
        base_local=base_id is not None,
    )


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
    resolved = resolve_overlay_image(manager, workspace, agent, base_image)
    if resolved is None:
        return base_image

    if rebuild or not resolved.built:
        info(
            f"Building overlay image {resolved.tag} from {base_image} ({resolved.dockerfile})",
        )
        # The workspace is identified by the opaque OVERLAY_KEY_LABEL only:
        # image metadata travels, and an absolute path would carry the host's
        # username and directory layout with it.
        labels = {
            "vibepod.managed": "true",
            OVERLAY_KEY_LABEL: resolved.key,
            "vibepod.overlay.agent": agent,
        }
        # A forced rebuild must also bypass docker's layer cache, or RUN
        # commands like package updates would replay from cached layers.
        manager.build_image(resolved.context_tar, tag=resolved.tag, labels=labels, nocache=rebuild)
        manager.remove_stale_overlays(resolved.key, keep_tag=resolved.tag)
    else:
        info(f"Using cached overlay image {resolved.tag}")
    return resolved.tag
