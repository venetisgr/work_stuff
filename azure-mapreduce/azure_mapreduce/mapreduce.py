"""Map-reduce over a DataFrame column, in the spirit of LangChain's MapReduceDocumentsChain.

Map: every record's text goes through ``map_prompt`` and the reply lands in an output column.
Reduce: the map outputs are taken ``reduce_group_size`` at a time, joined with ``separator`` and sent through
the reduce prompt; the replies are grouped and reduced again, level after level, until one text is left.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from tqdm.contrib.logging import logging_redirect_tqdm

from .client import AzureChatClient, Messages
from .errors import ConfigError, LLMRequestError, MapReduceError, StepFailedError
from .frames import as_text, column_values, to_frame
from .progress import StepProgress, levels_bar
from .runners import STRATEGIES, FallbackExecutor, LLMRequest, build_executor

log = logging.getLogger(__name__)

Prompt = str | Callable[[str], str]
OnError = Literal["warn", "raise"]


@dataclass(frozen=True)
class ReduceResult:
    """The final text, plus every level on the way to it.

    ``levels[0]`` holds the inputs (the map outputs), ``levels[1]`` the replies of the first reduce level,
    and so on; ``levels[-1] == [output]``.
    """

    output: str
    levels: list[list[str]]

    @property
    def depth(self) -> int:
        """How many reduce levels it took."""
        return len(self.levels) - 1


@dataclass(frozen=True)
class MapReduceResult:
    """What ``MapReduce.run`` returns: the DataFrame with the map outputs, and the reduced text."""

    frame: Any
    output: str
    levels: list[list[str]] = field(repr=False)
    map_failures: dict[int, LLMRequestError] = field(default_factory=dict, repr=False)

    @property
    def depth(self) -> int:
        return len(self.levels) - 1


def reduce_levels(count: int, group_size: int) -> int:
    """How many reduce levels ``count`` inputs need to get down to one text (at least one)."""
    levels = 1
    while count > group_size:
        count = math.ceil(count / group_size)
        levels += 1
    return levels


class MapReduce:
    """Map a prompt over a DataFrame column with Azure OpenAI, then reduce the outputs recursively.

    Requests go through ``strategies`` in order, by default the Batch API (50% cheaper, needs a batch
    deployment), then async chat completions, then a plain loop of chat completions; see the README.

    Prompts are strings with a ``{text}`` placeholder (other braces are left alone, so JSON examples are
    fine) or functions from the text to the prompt. ``collapse_prompt`` is used on the intermediate reduce
    levels and ``reduce_prompt`` on the last one; without a collapse prompt every level uses the reduce prompt.
    """

    def __init__(
        self,
        client: AzureChatClient,
        *,
        map_prompt: Prompt,
        reduce_prompt: Prompt,
        collapse_prompt: Prompt | None = None,
        system_prompt: str | None = None,
        map_batch_size: int = 100,
        reduce_group_size: int = 10,
        reduce_batch_size: int | None = None,
        strategies: Sequence[str] = STRATEGIES,
        max_concurrency: int = 16,
        batch_poll_interval: float = 30.0,
        batch_timeout: float | None = None,
        max_concurrent_batch_jobs: int = 4,
        batch_cleanup: bool = True,
        separator: str = "\n\n",
        placeholder: str = "{text}",
        on_error: OnError = "warn",
        show_progress: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        for name, prompt in (("map_prompt", map_prompt), ("reduce_prompt", reduce_prompt)):
            _check_prompt(name, prompt, placeholder)
        if collapse_prompt is not None:
            _check_prompt("collapse_prompt", collapse_prompt, placeholder)
        _check_positive("map_batch_size", map_batch_size)
        _check_positive("max_concurrency", max_concurrency)
        _check_positive("max_concurrent_batch_jobs", max_concurrent_batch_jobs)
        if reduce_batch_size is not None:
            _check_positive("reduce_batch_size", reduce_batch_size)
        if not isinstance(reduce_group_size, int) or reduce_group_size < 2:
            raise ConfigError(
                f"reduce_group_size must be 2 or more, or the reduce never ends (got {reduce_group_size!r})."
            )
        if batch_poll_interval <= 0:
            raise ConfigError(f"batch_poll_interval must be greater than zero (got {batch_poll_interval!r}).")
        if batch_timeout is not None and batch_timeout <= 0:
            raise ConfigError(f"batch_timeout must be greater than zero, or None to wait (got {batch_timeout!r}).")
        if on_error not in ("warn", "raise"):
            raise ConfigError(f'on_error must be "warn" or "raise" (got {on_error!r}).')

        self.client = client
        self.map_prompt = map_prompt
        self.reduce_prompt = reduce_prompt
        self.collapse_prompt = collapse_prompt if collapse_prompt is not None else reduce_prompt
        self.system_prompt = system_prompt
        self.map_batch_size = map_batch_size
        self.reduce_group_size = reduce_group_size
        self.reduce_batch_size = reduce_batch_size or map_batch_size
        self.separator = separator
        self.placeholder = placeholder
        self.on_error = on_error
        self.show_progress = show_progress
        self._executor_options = {
            "strategies": tuple(strategies),
            "max_concurrency": max_concurrency,
            "batch_poll_interval": batch_poll_interval,
            "batch_timeout": batch_timeout,
            "max_concurrent_batch_jobs": max_concurrent_batch_jobs,
            "batch_cleanup": batch_cleanup,
            "sleep": sleep,
            "clock": clock,
        }
        self._new_executor()  # check the strategy names now rather than halfway through a run

    # --- public API --------------------------------------------------------------------------------------

    def map(
        self,
        data: Any,
        column: str,
        output_column: str,
        *,
        error_column: str | None = None,
        id_column: str | None = None,
    ) -> Any:
        """Apply the map prompt to every record of ``column`` and return the DataFrame with ``output_column``.

        Works on pandas (returns a new DataFrame; the input is left alone) and PySpark DataFrames. Empty
        cells are skipped and get None, as do records that failed (with ``on_error="warn"``). Pass
        ``error_column`` to get each failed record's error in a column of its own. For Spark, ``id_column``
        (a column of unique ids) keeps the driver from collecting whole rows.
        """
        with self._logging():
            frame = to_frame(data, id_column=id_column)
            values = frame.values(column)
            outputs, failures = self._map(values, self._new_executor())
            return frame.with_outputs(output_column, outputs, error_column=error_column, failures=failures)

    def map_texts(self, texts: Sequence[Any]) -> list[str | None]:
        """The map step on a plain list: one output per text (None for empty or failed ones)."""
        with self._logging():
            outputs, _ = self._map(list(texts), self._new_executor())
            return outputs

    def reduce(self, data: Any, column: str | None = None) -> ReduceResult:
        """Reduce texts to one: a DataFrame column (e.g. the map output column), a Series, or a list.

        Empty values (such as records whose map step failed) are left out.
        """
        with self._logging():
            texts = [text for text in map(as_text, column_values(data, column)) if text is not None]
            return self._reduce(texts, self._new_executor())

    def run(
        self,
        data: Any,
        column: str,
        output_column: str,
        *,
        error_column: str | None = None,
        id_column: str | None = None,
    ) -> MapReduceResult:
        """Map then reduce: the DataFrame with the map outputs, and the single reduced text."""
        with self._logging():
            executor = self._new_executor()  # shared, so a strategy that broke in the map isn't retried
            frame = to_frame(data, id_column=id_column)
            outputs, failures = self._map(frame.values(column), executor)
            mapped = frame.with_outputs(output_column, outputs, error_column=error_column, failures=failures)
            reduced = self._reduce([text for text in outputs if text is not None], executor)
            return MapReduceResult(frame=mapped, output=reduced.output, levels=reduced.levels, map_failures=failures)

    # --- the two steps -----------------------------------------------------------------------------------

    def _map(
        self, values: Sequence[Any], executor: FallbackExecutor
    ) -> tuple[list[str | None], dict[int, LLMRequestError]]:
        requests = []
        for index, value in enumerate(values):
            text = as_text(value)
            if text is not None:
                requests.append(LLMRequest(index, self._messages(self.map_prompt, text)))
        skipped = len(values) - len(requests)
        if skipped:
            log.info("Skipping %d empty records.", skipped)
        with StepProgress(len(requests), "Map", unit="record", show=self.show_progress) as progress:
            results, failures = executor.run(requests, progress, batch_size=self.map_batch_size)
        outputs = [results.get(index) for index in range(len(values))]
        self._report_failures("map", "records", failures, outputs, len(requests))
        return outputs, failures

    def _reduce(self, texts: list[str], executor: FallbackExecutor) -> ReduceResult:
        if not texts:
            raise MapReduceError("Nothing to reduce: every input is empty (or failed in the map step).")
        size = self.reduce_group_size
        levels: list[list[str]] = [texts]
        current = texts
        total_levels = reduce_levels(len(current), size)
        with levels_bar(total_levels, show=self.show_progress) as bar:
            level = 0
            while True:
                level += 1
                groups = [current[start : start + size] for start in range(0, len(current), size)]
                final = len(groups) == 1
                prompt = self.reduce_prompt if final else self.collapse_prompt
                requests = [
                    LLMRequest(index, self._messages(prompt, self.separator.join(group)))
                    for index, group in enumerate(groups)
                ]
                label = f"Level {level}/{total_levels}: {len(current)} → {len(groups)}"
                bar.set_postfix_str(
                    f"level {level}: {_count(len(current), 'text')} in {_count(len(groups), 'group')} of up to {size}"
                )
                with StepProgress(
                    len(groups), label, unit="group", show=self.show_progress, position=1, leave=False
                ) as progress:
                    results, failures = executor.run(requests, progress, batch_size=self.reduce_batch_size)
                outputs = [results.get(index) for index in range(len(groups))]
                self._report_failures(f"reduce level {level}", "groups", failures, outputs, len(groups))
                current = [text for text in outputs if text is not None]
                if not current:
                    raise MapReduceError(f"Every group failed at reduce level {level}; see the warnings above.")
                levels.append(current)
                bar.update(1)
                if final:
                    break
                # Failed groups shrink the next level, which can save a level.
                remaining = reduce_levels(len(current), size)
                if level + remaining != total_levels:
                    total_levels = level + remaining
                    bar.total = total_levels
                    bar.refresh()
        return ReduceResult(output=current[0], levels=levels)

    # --- helpers -----------------------------------------------------------------------------------------

    def _messages(self, prompt: Prompt, text: str) -> Messages:
        content = prompt(text) if callable(prompt) else prompt.replace(self.placeholder, text)
        messages: Messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": content})
        return messages

    def _new_executor(self) -> FallbackExecutor:
        return build_executor(self.client, **self._executor_options)

    def _report_failures(
        self,
        step: str,
        noun: str,
        failures: Mapping[int, LLMRequestError],
        outputs: list[str | None],
        attempted: int,
    ) -> None:
        if not failures:
            return
        reasons = Counter(str(error) for error in failures.values())
        top = "; ".join(f"{reason} (×{count})" for reason, count in reasons.most_common(3))
        message = f"{len(failures)} of {attempted} {noun} failed in the {step}: {top}"
        if self.on_error == "raise":
            raise StepFailedError(message, outputs=outputs, failures=dict(failures))
        log.warning(message)

    @contextlib.contextmanager
    def _logging(self) -> Iterator[None]:
        """Route log messages through tqdm so they don't break the progress bars."""
        if not self.show_progress:
            yield
            return
        with logging_redirect_tqdm():
            yield


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _check_prompt(name: str, prompt: object, placeholder: str) -> None:
    if callable(prompt):
        return
    if not isinstance(prompt, str):
        raise ConfigError(f"{name} must be a string or a function of the text.")
    if placeholder not in prompt:
        raise ConfigError(f"{name} needs a {placeholder} placeholder where the text goes.")


def _check_positive(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{name} must be a whole number of at least 1 (got {value!r}).")
