"""
Tiny .env loader.

Loads `<repo_root>/.env` into os.environ on first import. Existing
environment values take precedence over .env (so a user can override
without editing the file). Idempotent — safe to import multiple times.

We avoid the python-dotenv dependency to keep the workbench's import
surface small. The .env grammar we accept is the simple subset:
    KEY=value          (no quoting / interpolation)
    # comment lines    ignored
    blank lines        ignored
"""

from __future__ import annotations

import os
from pathlib import Path


_LOADED = False


def load_dotenv(*, override: bool = False) -> int:
    """Load `.env` from the repo root into os.environ. Returns count of
    keys loaded. No-op on subsequent calls unless `override=True`."""
    global _LOADED
    if _LOADED and not override:
        return 0

    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    if not env_path.exists():
        _LOADED = True
        return 0

    n = 0
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip optional surrounding quotes
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if not key:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
                n += 1
    except Exception:
        pass

    _LOADED = True
    return n


# Auto-load on import — every loader in this codebase that reads env vars
# can `from core.env import load_dotenv; load_dotenv()` once at top level.
load_dotenv()
