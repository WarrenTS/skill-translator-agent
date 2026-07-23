from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path


class PathSecurityError(ValueError):
    pass


def resolve_within(root: Path, relative: str, *, must_exist: bool) -> Path:
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise PathSecurityError("path must be relative and cannot contain '..'")
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / requested).resolve(strict=must_exist)
    if not candidate.is_relative_to(resolved_root):
        raise PathSecurityError("path escapes its mounted root")
    return candidate


def atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path.name}")
    handle = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def artifact_record(path: Path, output_root: Path) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {
        "path": str(path.relative_to(output_root.resolve())),
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
