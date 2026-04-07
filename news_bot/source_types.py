from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import SourcePost


class SourceError(RuntimeError):
    pass


@dataclass(slots=True)
class SourceProbeResult:
    source_id: str
    source_name: str
    detail_lines: tuple[str, ...]


class SourceAdapter(Protocol):
    source_id: str
    source_name: str

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        ...

    def probe(self) -> SourceProbeResult:
        ...
