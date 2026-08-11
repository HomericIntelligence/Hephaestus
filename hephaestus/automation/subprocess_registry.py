"""Compatibility alias for the library-owned subprocess registry."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from hephaestus.utils import subprocess_registry as _implementation

if TYPE_CHECKING:
    from hephaestus.utils.subprocess_registry import (
        _register as _register,
        _unregister as _unregister,
        live_count as live_count,
        supported as supported,
        terminate_all as terminate_all,
        track_process_group as track_process_group,
    )
else:
    sys.modules[__name__] = _implementation
