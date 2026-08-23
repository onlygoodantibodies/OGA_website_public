"""Append-only audit log for every write/plan/apply across the servers.

An MCP connection is a standing credential. Whatever the trust level, we want a
durable record of what was asked and what happened. This is a dependency-free
JSONL sink (one JSON object per line) so it works locally with no extra
infrastructure; the owner can point it at a file on the Render disk (or swap in
a table) later.

Path: ``$MCP_AUDIT_LOG`` if set, else ``mcp_servers/audit.log`` in the repo.
Never raises into the caller — an audit failure must not break a real
operation, but it is surfaced on stderr.
"""
from __future__ import annotations

import json
import os
import sys
import threading

from .paths import MCP_DIR

_LOCK = threading.Lock()


def _log_path() -> str:
    return os.environ.get("MCP_AUDIT_LOG") or os.path.join(MCP_DIR, "audit.log")


def record(server: str, action: str, *, actor: str = "", ok: bool = True, **fields):
    """Append one audit entry.

    ``server``  — "A" | "B" | "C"
    ``action``  — e.g. "query", "add_antibodies.plan", "sql_apply"
    ``actor``   — the acting identity (member username / MCP identity), if known
    ``ok``      — whether the operation succeeded
    ``fields``  — any extra structured context (row counts, ids, token, etc.)

    Timestamp is added by the caller-independent clock. We avoid importing a
    wall clock at module import so tests stay deterministic where they need to.
    """
    from datetime import datetime, timezone

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "action": action,
        "actor": actor or "unknown",
        "ok": bool(ok),
    }
    # Keep values JSON-safe.
    for k, v in fields.items():
        try:
            json.dumps(v)
            entry[k] = v
        except TypeError:
            entry[k] = repr(v)

    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _LOCK:
            path = _log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:  # never break the real operation on a logging failure
        print(f"[audit] failed to write audit entry: {e}", file=sys.stderr)
    return entry
