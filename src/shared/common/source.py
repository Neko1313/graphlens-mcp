import asyncio
import json
from pathlib import Path

__all__ = ["is_file", "read_file", "read_span"]

_MAX_FILE_BYTES = 5_000_000


def _read_sync(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


async def is_file(root: Path, file_path: str) -> bool:
    """Whether a project file exists (path is root-relative)."""
    return await asyncio.to_thread((root / file_path).is_file)


async def read_file(root: Path, file_path: str) -> str | None:
    """Read a project file off the event loop, capped at a few MB. None if
    unreadable or too large.
    """
    return await asyncio.to_thread(_read_sync, root / file_path)


async def read_span(
    root: Path,
    file_path: str | None,
    span_json: str | None,
) -> tuple[str, str]:
    """Return ``(source, signature)`` for a node.

    ``source`` is the node's body sliced by its 1-based span; ``signature`` is
    its first line (graphlens stores no ready-made signature string). Both are
    empty when the node has no file/span or the file can't be read.
    """
    if not file_path:
        return "", ""
    text = await read_file(root, file_path)
    if text is None:
        return "", ""
    lines = text.splitlines()
    if not span_json:
        return text, lines[0] if lines else ""
    start_line, _, end_line, _ = json.loads(span_json)
    body = lines[max(start_line - 1, 0) : end_line]
    source = "\n".join(body)
    return source, body[0].strip() if body else ""
