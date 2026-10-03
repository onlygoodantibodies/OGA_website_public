"""Is this checkout the public snapshot, and did the export leave a file out?

The public repository is built by ``tools/export_public.py``, which leaves out
files that carry private data (named contacts, the lab's session rows) or are
not source (the skills). A handful of tests read one of those files, so in the
public copy they failed for a reason that says nothing about the code. They
skip there instead — and only there: the public copy is recognised by the
licence file the export puts at its root (``publish/LICENSE-DATA`` here), so in
this repo a missing file still fails loudly. No Django import, so the MCP suite
can ask it too.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def is_public_snapshot() -> bool:
    return (ROOT / "LICENSE-DATA").is_file()


def withheld(path) -> bool:
    """True when ``path`` is missing because the public export left it out."""
    return is_public_snapshot() and not Path(path).exists()


REASON = "left out of the public snapshot by tools/export_public.py"
