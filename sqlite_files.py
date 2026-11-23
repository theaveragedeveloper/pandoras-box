"""Open offline indexes read-only and publish complete rebuilds."""

import os
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path


def open_readonly(path):
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)


def _fsync_file(path):
    """Flush a file's contents to disk (best-effort on Windows)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_dir(path):
    """Flush a directory entry to disk; POSIX only (no-op on Windows)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def rebuild_index(path):
    """Leave the current database in place until the new one is complete.

    Index rebuilds should be done with the hub stopped: on Windows
    os.replace fails while the hub holds the target open, and the
    retry below only covers a transient open handle, not a hub that
    keeps the file open. The staging DB is verified (integrity_check
    + at least one table with rows) before it replaces the live one,
    so a builder that produced an empty/broken DB never publishes.
    """
    target = Path(path).absolute()
    with tempfile.TemporaryDirectory(prefix=".index-build-", dir=target.parent) as directory:
        staging = Path(directory, target.name)
        with closing(sqlite3.connect(staging)) as connection:
            yield connection
            connection.commit()
        publish_index(staging, target)


def publish_index(staging, target):
    """Validate an index and atomically make it the live file."""
    staging, target = Path(staging), Path(target)
    # Verify what the builder produced before publishing it.
    with closing(sqlite3.connect(staging)) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise ValueError(
                f"rebuilt index failed integrity_check: {integrity}"
            )
        tables = check.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not tables:
            raise ValueError("rebuilt index has no tables; refusing to publish")
        rows = 0
        for (name,) in tables:
            # FTS5 shadow tables (name_data/content/...) hold no
            # user rows of their own; count the real tables only.
            if name.endswith(("_data", "_content", "_segments",
                               "_segdir", "_docsize", "_config")):
                continue
            identifier = '"' + name.replace('"', '""') + '"'
            rows += check.execute(f"SELECT COUNT(*) FROM {identifier}").fetchone()[0]
        if rows == 0:
            raise ValueError(
                "rebuilt index has no rows; refusing to publish"
            )
    _fsync_file(staging)
    original_mode = None
    if target.exists():
        try:
            original_mode = os.stat(target).st_mode
        except OSError:
            original_mode = None
    last_error = None
    for attempt in range(5):
        try:
            os.replace(staging, target)
            break
        except PermissionError as exc:
            # Windows: the target may still be open by a transient
            # reader (e.g. the hub's read-only connection). Retry
            # briefly; a hub that holds it open needs stopping.
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    else:
        raise PermissionError(
            "could not replace the live index (is the hub still "
            f"running and holding {target} open?): {last_error}"
        )
    if original_mode is not None:
        try:
            os.chmod(target, original_mode)
        except OSError:
            pass
    _fsync_dir(target.parent)
