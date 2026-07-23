from pathlib import Path

import pytest

from translator_agent.security import PathSecurityError, atomic_write, resolve_within


def test_resolve_within_rejects_traversal(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathSecurityError):
        resolve_within(root, "../secret.txt", must_exist=False)


def test_resolve_within_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSecurityError):
        resolve_within(root, "escape/file.txt", must_exist=False)


def test_atomic_write_refuses_overwrite(tmp_path: Path):
    path = tmp_path / "output.txt"
    atomic_write(path, b"one", overwrite=False)
    with pytest.raises(FileExistsError):
        atomic_write(path, b"two", overwrite=False)
    assert path.read_bytes() == b"one"
