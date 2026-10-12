"""Explicit destructive initialization of a stopped OURO home."""

from __future__ import annotations

import shutil
from pathlib import Path

from ouro.auth.file_permissions import reject_link
from ouro.local_io import write_json_atomic
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.progress import ProgressCallback
from ouro_upgrade.workspace import Workspace, sync_directory


def reset_home(home: str | Path, *, progress: ProgressCallback | None = None) -> None:
    """Clear all home contents after caller confirmation; retain only the maintenance lock.

    Other writers must be stopped. An interrupted reset remains blocked and can only
    be completed by another explicit reset, not by restoring upgrade backups.
    """
    root = Path(home).expanduser().absolute()
    # An accidentally broad --home must never erase the user profile or working tree.
    protected = {Path.home(), *Path.home().parents, Path.cwd(), *Path.cwd().parents}
    if root.resolve() in protected:
        raise UpgradeError("refusing to initialize a root, user profile or working directory")
    try:
        with Workspace(root, progress=progress) as work:
            children = [path for path in root.iterdir() if path != work.root]
            children.extend(
                path for path in work.root.iterdir() if path not in {work.lock.path, work.marker}
            )
            reject_link(work.marker)
            for path in children:
                reject_link(path)
            write_json_atomic(work.marker, {"operation": "reset"})
            sync_directory(work.root)
            work.progress("reset", 0, len(children))
            for index, path in enumerate(children):
                # Paths come only from these two owned directories. rmtree does not
                # traverse nested symbolic links or Windows directory junctions.
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                work.progress("reset", index + 1, len(children))
            sync_directory(root)
            work.marker.unlink()
            sync_directory(work.root)
            work.progress("complete")
    except OSError as exc:
        raise UpgradeError(
            f"initialization failed ({type(exc).__name__}); "
            "stop other OURO processes and retry initialization"
        ) from exc
