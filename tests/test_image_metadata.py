"""Image metadata collection tests."""

from __future__ import annotations

from vibepod.core.image_metadata import ImageMetadata, collect_image_metadata


class _FakeImage:
    def __init__(self, image_id: str | None = None, labels: dict | None = None) -> None:
        self.id = image_id
        self.labels = labels or {}


class _FakeContainer:
    def __init__(self, image: object | None = None, attrs: dict | None = None) -> None:
        if image is not None:
            self.image = image
        self.attrs = attrs or {}


class _ExplodingImageContainer:
    """Container whose ``image`` property raises, like docker-py on a lost daemon."""

    attrs: dict = {}

    @property
    def image(self) -> object:
        raise RuntimeError("daemon gone")


def test_collects_tag_hash_and_agent_version() -> None:
    image = _FakeImage(
        image_id="sha256:" + "a" * 64,
        labels={"vibepod.agent.version": "2.1.0"},
    )
    container = _FakeContainer(image=image)

    meta = collect_image_metadata(container, "vibepod/claude:0.18.0")

    assert meta == ImageMetadata(
        image_tag="0.18.0",
        image_hash="sha256:" + "a" * 64,
        agent_version="2.1.0",
    )


def test_untagged_reference_yields_no_tag() -> None:
    meta = collect_image_metadata(_FakeContainer(), "vibepod/claude")

    assert meta.image_tag is None


def test_digest_reference_keeps_digest_as_tag() -> None:
    digest = "sha256:" + "b" * 64
    meta = collect_image_metadata(_FakeContainer(), f"vibepod/claude@{digest}")

    assert meta.image_tag == digest


def test_registry_port_not_mistaken_for_tag() -> None:
    meta = collect_image_metadata(_FakeContainer(), "localhost:5000/vibepod/claude")

    assert meta.image_tag is None


def test_hash_falls_back_to_container_attrs() -> None:
    container = _FakeContainer(attrs={"Image": "sha256:" + "c" * 64})

    meta = collect_image_metadata(container, "vibepod/claude:latest")

    assert meta.image_hash == "sha256:" + "c" * 64


def test_agent_version_falls_back_to_oci_label() -> None:
    image = _FakeImage(labels={"org.opencontainers.image.version": "1.5.0"})

    meta = collect_image_metadata(_FakeContainer(image=image), "vibepod/claude:latest")

    assert meta.agent_version == "1.5.0"


def test_vibepod_label_wins_over_oci_label() -> None:
    image = _FakeImage(
        labels={
            "org.opencontainers.image.version": "1.5.0",
            "vibepod.agent.version": "2.0.0",
        }
    )

    meta = collect_image_metadata(_FakeContainer(image=image), "vibepod/claude:latest")

    assert meta.agent_version == "2.0.0"


def test_missing_everything_yields_all_none_except_tag() -> None:
    meta = collect_image_metadata(_FakeContainer(), "vibepod/claude:latest")

    assert meta == ImageMetadata(image_tag="latest", image_hash=None, agent_version=None)


def test_broken_image_property_is_swallowed() -> None:
    meta = collect_image_metadata(_ExplodingImageContainer(), "vibepod/claude:latest")

    assert meta.image_hash is None
    assert meta.agent_version is None


def test_non_string_label_ignored() -> None:
    image = _FakeImage(labels={"vibepod.agent.version": 7})

    meta = collect_image_metadata(_FakeContainer(image=image), "vibepod/claude:latest")

    assert meta.agent_version is None


def test_broken_labels_property_is_swallowed() -> None:
    class _BrokenLabelsImage:
        id = "sha256:" + "f" * 64

        @property
        def labels(self) -> dict:
            raise KeyError("Config")

    meta = collect_image_metadata(
        _FakeContainer(image=_BrokenLabelsImage()), "vibepod/claude:latest"
    )

    assert meta.image_hash == "sha256:" + "f" * 64
    assert meta.agent_version is None


def test_broken_id_attribute_is_swallowed() -> None:
    class _BrokenIdImage:
        labels = {"vibepod.agent.version": "1.0.0"}

        @property
        def id(self) -> str:
            raise RuntimeError("daemon gone")

    container = _FakeContainer(
        image=_BrokenIdImage(), attrs={"Image": "sha256:" + "9" * 64}
    )

    meta = collect_image_metadata(container, "vibepod/claude:latest")

    assert meta.image_hash == "sha256:" + "9" * 64
