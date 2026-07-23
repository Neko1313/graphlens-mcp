import asyncio
import json
from pathlib import Path

__all__ = ["is_file", "read_file", "read_span"]

_MAX_FILE_BYTES = 5_000_000


def _resolve_within(root: Path, file_path: str) -> Path | None:
    """Resolve ``file_path`` under ``root``, refusing any escape.

    ``root / file_path`` treats an absolute ``file_path`` as a full override
    (``Path("/a") / "/etc/passwd" == Path("/etc/passwd")``) and does nothing
    to stop ``../`` traversal, so a caller-controlled path must be checked
    against the resolved root before it ever reaches the filesystem.
    """
    candidate = (root / file_path).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def _read_sync(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


async def is_file(root: Path, file_path: str) -> bool:
    """Whether a project file exists (path is root-relative)."""
    resolved = _resolve_within(root, file_path)
    if resolved is None:
        return False
    return await asyncio.to_thread(resolved.is_file)


async def read_file(root: Path, file_path: str) -> str | None:
    """Read a project file off the event loop, capped at a few MB. None if
    unreadable, too large, or outside ``root``.
    """
    resolved = _resolve_within(root, file_path)
    if resolved is None:
        return None
    return await asyncio.to_thread(_read_sync, resolved)


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
