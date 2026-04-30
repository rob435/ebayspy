from __future__ import annotations

from pathlib import Path


if Path("graphify-out/graph.json").is_file():
    # Older stack templates emitted hookSpecificOutput.additionalContext here,
    # but this Codex runtime rejects that field. Keep the hook successful and
    # rely on AGENTS.md for the Graphify reminder.
    pass
