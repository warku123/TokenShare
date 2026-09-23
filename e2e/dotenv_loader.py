#!/usr/bin/env python3
"""Minimal stdlib .env loader shared by the e2e entry points.

Rules (deliberately tiny, no python-dotenv dependency):
  - looks for `.env` at the repo root (two levels up from this file);
  - skips blank lines and `#` comments;
  - parses `KEY=VALUE` (first `=` splits; VALUE stripped, surrounding
    single/double quotes stripped);
  - NEVER overwrites an existing os.environ entry (real env wins);
  - silently does nothing when the file is missing;
  - never prints or logs any value (private-key discipline).
"""

from __future__ import annotations

import os
from pathlib import Path

_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_dotenv(path: Path = _DOTENV_PATH) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return  # no .env → nothing to do (silent)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Quoted value: `KEY="v # w"  # note` — everything after the closing
        # quote must be blank or a comment, else treat as unquoted text.
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            if closing != -1 and value[closing + 1:].strip().startswith(("", "#")):
                value = value[1:closing]
            elif closing == -1:
                value = value[1:]
        else:
            # Unquoted: `#` at value start = empty value + comment; ` #`
            # = inline comment (dotenv convention). Keys/addresses/amounts
            # never legitimately contain ' #'.
            if value.startswith("#"):
                value = ""
            elif " #" in value:
                value = value.split(" #", 1)[0]
        value = value.strip()
        if not value:
            continue  # empty (or comment-only) value → do not pollute os.environ
        if key not in os.environ:
            os.environ[key] = value
