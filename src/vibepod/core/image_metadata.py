"""Best-effort collection of container image metadata for sqlite records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from vibepod.core.docker import _parse_image_name

AGENT_VERSION_LABELS: Final = (
    "vibepod.agent.version",
    "org.opencontainers.image.version",
)


@dataclass(frozen=True)
class ImageMetadata:
    """Image provenance persisted alongside session and task rows."""

    image_tag: str | None
    image_hash: str | None
    agent_version: str | None


def collect_image_metadata(container: object, image: str) -> ImageMetadata:
    """Extract tag, hash, and agent version for the image behind *container*.

    Everything is best-effort: agent images without version labels, stub
    containers in tests, or a daemon that dropped away mid-inspect must never
    break the launch — missing pieces simply become ``None``.
    """
    _, image_tag = _parse_image_name(image)

    image_hash: str | None = None
    agent_version: str | None = None
    # docker-py's Container.image and Image.labels are properties backed by API
    # calls and raw inspect payloads — each access can raise (KeyError included,
    # which getattr defaults do not swallow), so every read stays guarded.
    try:
        image_obj = getattr(container, "image", None)
    except Exception:
        image_obj = None
    if image_obj is not None:
        try:
            image_id = getattr(image_obj, "id", None)
        except Exception:
            image_id = None
        if isinstance(image_id, str) and image_id:
            image_hash = image_id
        try:
            labels = getattr(image_obj, "labels", None)
        except Exception:
            labels = None
        if isinstance(labels, dict):
            for label in AGENT_VERSION_LABELS:
                value = labels.get(label)
                if isinstance(value, str) and value:
                    agent_version = value
                    break

    if image_hash is None:
        attrs = getattr(container, "attrs", None)
        if isinstance(attrs, dict):
            attr_image = attrs.get("Image")
            if isinstance(attr_image, str) and attr_image:
                image_hash = attr_image

    return ImageMetadata(image_tag=image_tag, image_hash=image_hash, agent_version=agent_version)
