"""Explicit snapshot backup and restoration; execute through `ouro plugin maintain`."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from scheduled_sync_plugin import validate_snapshot


def backup_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(Path.cwd().resolve()):
        raise ValueError("Backups must be outside the plugin data directory")
    return path


def save_backup(destination: Path, content: bytes) -> None:
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    backup = actions.add_parser("backup")
    backup.add_argument("destination", type=backup_path)
    restore = actions.add_parser("restore")
    restore.add_argument("source", type=backup_path)
    restore.add_argument("--save-current", type=backup_path, required=True)
    args = parser.parse_args()
    snapshot = Path("snapshot.json")
    if args.action == "backup":
        save_backup(args.destination, snapshot.read_bytes())
        return
    content = args.source.read_bytes()
    validate_snapshot(content)
    if snapshot.exists():
        save_backup(args.save_current, snapshot.read_bytes())
    stream = tempfile.NamedTemporaryFile(dir=Path.cwd(), delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, snapshot)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
