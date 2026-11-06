from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

Progress = Callable[[str], None]


class Destination(Protocol):
    id: str

    def push(self, ms: str, staging: Path, manifest: dict, progress: Progress) -> dict:
        """Store the staged snapshot; return stats. The manifest must become visible last."""

    def snapshots(self, ms: str) -> list[dict]:
        """Manifest summaries, newest first."""

    def manifest(self, ms: str, snapshot: str) -> dict: ...

    def open(self, ms: str, snapshot: str, path: str, manifest: dict) -> BinaryIO: ...

    def delete(self, ms: str, snapshot: str) -> None: ...

    def retention(self, ms: str, keep: int) -> list[str]:
        """Delete all but the newest `keep` snapshots; return deleted names."""

    def gc(self, ms: str, progress: Progress) -> dict: ...

    def verify(self, ms: str, snapshot: str, deep: bool) -> list[str]:
        """Problems found (empty = ok)."""

    def test(self) -> str: ...

    def miniservers(self) -> Iterator[str]: ...
