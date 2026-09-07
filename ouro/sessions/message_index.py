from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock

from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json


@dataclass
class _JournalIndex:
    identity: tuple[int, int] | None = None
    size: int = 0
    tail_offset: int = 0
    tail_digest: bytes = b""
    messages: dict[str, int] = field(default_factory=dict)
    queued: dict[str, int] = field(default_factory=dict)
    runs: dict[str, list[int]] = field(default_factory=dict)

    def update(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open("rb") as stream:
            stat = path.stat()
            identity = stat.st_dev, stat.st_ino
            stream.seek(self.tail_offset)
            if (
                identity != self.identity
                or stat.st_size < self.size
                or (self.size and sha256(stream.readline()).digest() != self.tail_digest)
            ):
                self.size = 0
                self.tail_offset = 0
                self.tail_digest = b""
                self.messages.clear()
                self.queued.clear()
                self.runs.clear()
            stream.seek(self.size)
            last_line = None
            while line := stream.readline():
                offset = self.size
                self.size += len(line)
                record = json.loads(line)
                self.messages[record["message_id"]] = offset
                queue_id = record.get("metadata", {}).get("queued_input_id")
                if isinstance(queue_id, str):
                    self.queued[queue_id] = offset
                run_id = record.get("run_id")
                if isinstance(run_id, str):
                    self.runs.setdefault(run_id, []).append(offset)
                self.tail_offset = offset
                last_line = line
            self.identity = identity
            if last_line is not None:
                self.tail_digest = sha256(last_line).digest()


class MessageJournalQueries:
    """Disposable offset indexes for a small set of queried Session journals.

    Callers hold the Session mutation lock. First access indexes that journal;
    subsequent reads inspect only its appended suffix and the requested records.
    A digest of the last indexed record detects suffix replacement even when
    size and trailing fields are unchanged. Normal appends still index only new
    records. Cached data contains offsets and one digest, never message bodies.
    """

    def __init__(self) -> None:
        self._indexes: OrderedDict[Path, _JournalIndex] = OrderedDict()
        self._lock = Lock()

    def _index(self, path: Path) -> _JournalIndex:
        with self._lock:
            index = self._indexes.pop(path, None) or _JournalIndex()
            self._indexes[path] = index
            if len(self._indexes) > 8:
                self._indexes.popitem(last=False)
        try:
            index.update(path)
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            with self._lock:
                self._indexes.pop(path, None)
            raise PersistenceError(f"could not index message journal {path}: {exc}") from exc
        return index

    @staticmethod
    def _read(path: Path, offsets: list[int]) -> list[MessageRecord]:
        if not offsets:
            return []
        try:
            with path.open("rb") as stream:
                messages = []
                for offset in offsets:
                    stream.seek(offset)
                    messages.append(
                        decode_versioned_record_json(
                            MessageRecord, stream.readline(), label=f"message in {path}"
                        )
                    )
                return messages
        except (OSError, ValueError) as exc:
            raise PersistenceError(f"could not read message journal {path}: {exc}") from exc

    def message(self, path: Path, message_id: str) -> MessageRecord | None:
        offset = self._index(path).messages.get(message_id)
        return self._read(path, [offset])[0] if offset is not None else None

    def queued(self, path: Path, queue_id: str) -> MessageRecord | None:
        offset = self._index(path).queued.get(queue_id)
        return self._read(path, [offset])[0] if offset is not None else None

    def run(self, path: Path, run_id: str) -> list[MessageRecord]:
        return self._read(path, self._index(path).runs.get(run_id, []))
