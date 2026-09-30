"""tqdm progress bars: one for the map step, and for the reduce step one per level inside a bar over the levels."""

from __future__ import annotations

import threading
from typing import Any

from tqdm.auto import tqdm


class StepProgress:
    """A bar over the requests of one step (the map, or one reduce level).

    The description names the strategy doing the work (``Map [batch]``), and the postfix shows where it is
    (which chunk or batch job) and how many requests failed for good. Runners call it from worker threads,
    so updates are locked.
    """

    def __init__(self, total: int, label: str, *, unit: str, show: bool = True, position: int = 0, leave: bool = True):
        self.label = label
        self.failed = 0
        self._note = ""
        self._lock = threading.Lock()
        self._bar = tqdm(
            total=total, desc=label, unit=unit, disable=not show, position=position, leave=leave, dynamic_ncols=True
        )

    @property
    def done(self) -> int:
        return int(self._bar.n)

    def strategy(self, name: str) -> None:
        with self._lock:
            self._bar.set_description_str(f"{self.label} [{name}]", refresh=False)
            self._note = ""
            self._render()

    def advance(self, count: int = 1) -> None:
        """Count requests that got a result (a negative count takes back an estimate)."""
        if count:
            with self._lock:
                self._bar.update(count)

    def fail(self, count: int = 1) -> None:
        """Count requests that failed for good, so the bar still reaches the end."""
        with self._lock:
            self.failed += count
            self._bar.update(count)
            self._render()

    def note(self, text: str) -> None:
        with self._lock:
            self._note = text
            self._render()

    def close(self) -> None:
        self._bar.close()

    def _render(self) -> None:
        parts = [self._note] if self._note else []
        if self.failed:
            parts.append(f"failed={self.failed}")
        self._bar.set_postfix_str(" · ".join(parts), refresh=True)

    def __enter__(self) -> StepProgress:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class NullProgress(StepProgress):
    """A progress sink that shows nothing, for callers that don't want a bar."""

    def __init__(self, total: int = 0, label: str = "", **_: Any):
        super().__init__(total, label, unit="it", show=False)


def levels_bar(total: int, *, show: bool) -> tqdm:
    """The outer bar of the reduce step: one tick per recursion level."""
    return tqdm(total=total, desc="Reduce", unit="level", disable=not show, position=0, leave=True, dynamic_ncols=True)
