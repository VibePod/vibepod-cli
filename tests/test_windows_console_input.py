"""Windows console input (issue #242); everything here runs off Windows."""

from __future__ import annotations

import ctypes
import types

from vibepod.core import docker as docker_mod


def test_kernel32_prototypes_keep_handles_pointer_sized() -> None:
    names = [
        "GetStdHandle",
        "GetConsoleMode",
        "SetConsoleMode",
    ]
    fake = types.SimpleNamespace(**{name: types.SimpleNamespace() for name in names})

    docker_mod._declare_kernel32_prototypes(fake)

    assert fake.GetStdHandle.restype is ctypes.c_void_p
    for name in names[1:]:
        assert fake.__dict__[name].argtypes[0] is ctypes.c_void_p, name
    assert fake.SetConsoleMode.argtypes[1] is ctypes.c_uint32
