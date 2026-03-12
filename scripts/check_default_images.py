#!/usr/bin/env python3
"""Validate that built-in default container images exist in registries."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

MANIFEST_CHECK_TIMEOUT_SECONDS = 30


def _unset_image_env_overrides(image_override_env_keys: tuple[str, ...]) -> None:
    """Force canonical built-in defaults by clearing image override env vars."""
    for key in image_override_env_keys:
        os.environ.pop(key, None)


def _check_image_exists(image: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "manifest", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=MANIFEST_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            (
                "timed out after "
                f"{MANIFEST_CHECK_TIMEOUT_SECONDS}s while checking docker manifest"
            ),
        )
    if proc.returncode == 0:
        return True, ""
    error = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
    return False, error


def main() -> int:
    if shutil.which("docker") is None:
        print("Error: docker CLI is required but not found in PATH.", file=sys.stderr)
        return 1

    from vibepod.constants import IMAGE_OVERRIDE_ENV_KEYS, get_default_images

    _unset_image_env_overrides(IMAGE_OVERRIDE_ENV_KEYS)
    default_images = get_default_images()

    failures: list[str] = []

    for name in sorted(default_images):
        image = default_images[name]
        print(f"Checking default image for {name}: {image}")
        ok, error = _check_image_exists(image)
        if ok:
            continue
        failures.append(f"- {name}: {image}\n  {error}")

    if failures:
        print("\nDefault image validation failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1

    print("\nAll default images are resolvable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
