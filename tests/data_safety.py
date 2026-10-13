"""Exact-byte guards for tests that exercise persistent project JSON files."""

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERSISTENT_FILES = (
    PROJECT_ROOT / "data" / "application_queue.json",
    PROJECT_ROOT / "data" / "applications.json",
)


def _restore(path, original):
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".snapshot.tmp", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


@contextmanager
def preserve_project_data():
    """Restore exact original bytes and fail if a test touched project data."""
    before = {
        path: path.read_bytes() if path.exists() else None
        for path in PERSISTENT_FILES
    }
    changed = []
    try:
        yield
    finally:
        for path, original in before.items():
            current = path.read_bytes() if path.exists() else None
            if current != original:
                changed.append(path)
                _restore(path, original)
        if changed and sys.exc_info()[0] is None:
            names = ", ".join(str(path.relative_to(PROJECT_ROOT)) for path in changed)
            raise AssertionError(f"Test changed persistent project data; exact bytes restored: {names}")


def preserve_project_data_test(test_function):
    """Decorator form for existing fixture/integration unittest methods."""
    @wraps(test_function)
    def guarded(*args, **kwargs):
        with preserve_project_data():
            return test_function(*args, **kwargs)
    return guarded
