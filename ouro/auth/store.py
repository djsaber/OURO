from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.auth.contracts import CREDENTIAL_ID_PATTERN, AuthError, CredentialRecord
from ouro.local_io import read_text_atomic, replace_path_with_retry
from ouro.local_locks import try_lock_descriptor, unlock_descriptor
from ouro.local_permissions import reject_link, restrict_to_user


class FileCredentialStore:
    """Restricted cleartext records; atomic replacement and locks have separate files."""

    def __init__(self, home: Path) -> None:
        self.directory = home / "credentials"

    def _path(self, credential_id: str, suffix: str = ".json") -> Path:
        if re.fullmatch(CREDENTIAL_ID_PATTERN, credential_id) is None:
            raise AuthError("credential_store_error")
        return self.directory / f"{credential_id}{suffix}"

    def _prepare_directory(self) -> None:
        reject_link(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        restrict_to_user(self.directory, directory=True)

    def read(self, credential_id: str) -> CredentialRecord | None:
        path = self._path(credential_id)
        try:
            reject_link(self.directory)
            reject_link(path)
            record = CredentialRecord.model_validate_json(read_text_atomic(path))
            if (
                record.credential_id != credential_id
                or "format_version" not in record.model_fields_set
            ):
                raise AuthError("credential_store_error")
            return record
        except FileNotFoundError:
            return None
        except OSError, ValueError:
            # Validation and JSON exceptions may contain secrets, even partial JSON.
            raise AuthError("credential_store_error") from None

    def write(self, record: CredentialRecord) -> None:
        path = self._path(record.credential_id)
        temporary: Path | None = None
        try:
            self._prepare_directory()
            reject_link(path)
            with NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=self.directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                restrict_to_user(temporary)
                stream.write(record.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            replace_path_with_retry(temporary, path)
        except OSError, ValueError:
            raise AuthError("credential_store_error") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise AuthError("credential_store_error") from None

    def list_ids(self) -> tuple[str, ...]:
        try:
            reject_link(self.directory)
            return tuple(
                sorted(
                    path.stem
                    for path in self.directory.iterdir()
                    if path.suffix == ".json"
                    and re.fullmatch(CREDENTIAL_ID_PATTERN, path.stem) is not None
                )
            )
        except FileNotFoundError:
            return ()
        except OSError:
            raise AuthError("credential_store_error") from None

    @asynccontextmanager
    async def lock(self, credential_id: str, *, timeout: float) -> AsyncGenerator[None]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("credential lock timeout must be positive")
        path = self._path(credential_id, ".lock")
        descriptor: int | None = None
        acquired = False
        try:
            # These non-blocking lock syscalls never wait for a peer. Record I/O is
            # dispatched by CredentialService; contention yields to the event loop.
            try:
                deadline = asyncio.get_running_loop().time() + timeout
                self._prepare_directory()
                reject_link(path)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                while not (acquired := try_lock_descriptor(descriptor)):
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise AuthError("credential_lock_timeout")
                    await asyncio.sleep(min(0.025, remaining))
            except OSError:
                raise AuthError("credential_store_error") from None
            yield
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        unlock_descriptor(descriptor)
                finally:
                    os.close(descriptor)
