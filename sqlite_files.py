"""Open offline indexes read-only and publish complete rebuilds."""

import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path


def open_readonly(path):
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)


@contextmanager
def rebuild_index(path):
    """Leave the current database in place until the new one is complete."""
    target = Path(path).absolute()
    with tempfile.TemporaryDirectory(prefix=".index-build-", dir=target.parent) as directory:
        staging = Path(directory, target.name)
        with closing(sqlite3.connect(staging)) as connection:
            yield connection
            connection.commit()
        os.replace(staging, target)
