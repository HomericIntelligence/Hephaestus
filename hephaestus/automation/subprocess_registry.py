"""Compatibility alias for the library-owned subprocess registry."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from hephaestus.utils import subprocess_registry as _implementation

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    _register: Callable[[int], None]
    _unregister: Callable[[int], None]
    live_count: Callable[[], int]
    supported: Callable[[], bool]
    terminate_all: Callable[..., int]
    track_process_group: Callable[[int], AbstractContextManager[None]]
else:
    sys.modules[__name__] = _implementation
