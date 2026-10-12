"""Snapshot private data with a consistent SQLite copy while app is running."""
import sqlite3
import os
import sys
import tarfile
import tempfile
from pathlib import Path


def snapshot(data_dir, output):
    data_dir = Path(data_dir)
    with tempfile.TemporaryDirectory(prefix="gg-backup-") as folder:
        database = data_dir / "max-bot.sqlite"
        copy = Path(folder) / "max-bot.sqlite"
        if database.exists():
            source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(copy, 0o600)
        def exclude(info):
            name = Path(info.name).name
            return None if name.startswith("max-bot.sqlite") or name.endswith(".tmp") else info
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(data_dir.iterdir()):
                if path.name.startswith("max-bot.sqlite") or path.name.endswith(".tmp"):
                    continue
                archive.add(path, arcname="./" + path.name, filter=exclude)
            if copy.exists():
                archive.add(copy, arcname="./max-bot.sqlite")


if __name__ == "__main__":
    snapshot(sys.argv[1] if len(sys.argv) > 1 else "/data",
             sys.argv[2] if len(sys.argv) > 2 else "/dump/snapshot.tar.gz")
