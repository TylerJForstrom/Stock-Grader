"""Cross-platform cache locations shared by every data provider."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

__all__ = ["default_cache_dir", "safe_cache_path"]

_APP_DIR = "stock-grader"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^_-]{0,127}$")


def _is_windows() -> bool:
    return os.name == "nt"


def default_cache_dir(*parts: str) -> Path:
    """Return the platform-native per-user cache directory.

    Windows applications belong under ``%LOCALAPPDATA%`` rather than a Unix-style ``~/.cache``
    directory. Unix-like systems honor ``$XDG_CACHE_HOME`` and otherwise retain the conventional
    ``~/.cache`` fallback.
    """
    if _is_windows():
        configured = os.environ.get("LOCALAPPDATA")
        base = Path(configured).expanduser() if configured else Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_CACHE_HOME")
        base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base.joinpath(_APP_DIR, *parts)


def safe_cache_path(root: Path, namespace: str, identifier: str, suffix: str) -> Path:
    """Build a deterministic cache filename that cannot escape ``root``.

    Both slash styles are unsafe input: a backslash is an ordinary filename character on Unix but
    a path separator on Windows. Unsafe identifiers are replaced with a stable digest so callers
    retain caching without retaining attacker-controlled path syntax.
    """
    root = root.resolve()
    raw = str(identifier).strip()
    if _SAFE_IDENTIFIER.fullmatch(raw) and ".." not in raw:
        component = raw
    else:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        component = f"id-{digest}"
    prefix = f"{namespace}_" if namespace else ""
    candidate = (root / f"{prefix}{component}{suffix}").resolve()
    if candidate.parent != root:
        raise ValueError("cache path escaped its configured directory")
    return candidate
