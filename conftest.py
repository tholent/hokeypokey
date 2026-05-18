"""Root conftest.py — applies compatibility shims before any imports."""

from __future__ import annotations

import sys
import types

# pgpy 0.6.0 uses `imghdr` which was removed in Python 3.13.
# Inject a minimal shim so pgpy can be imported.
if "imghdr" not in sys.modules:
    _imghdr = types.ModuleType("imghdr")
    _imghdr.what = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["imghdr"] = _imghdr
