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

from .client import AzureChatClient, Messages
from .errors import ConfigError, LLMRequestError, MapReduceError, StepFailedError, attach
from .frames import Frame, as_text, column_values, is_pandas_dataframe, is_spark_dataframe, to_frame
from .progress import StepProgress, bar_safe_logging, levels_bar
from .runners import _CANCEL_GRACE_SECONDS, STRATEGIES, FallbackExecutor, LLMRequest, build_executor

log = logging.getLogger(__name__)

Prompt = str | Callable[[str], str]
OnError = Literal["warn", "raise"]
ReduceFailures = dict[int, dict[int, LLMRequestError]]  # level -> group index -> error


@dataclass(frozen=True)
class ReduceResult:
    """The final text, plus every level on the way to it.

    ``levels[0]`` holds the inputs (the map outputs), ``levels[1]`` the replies of the first reduce level,
    and so on; ``levels[-1] == [output]``. ``failures`` lists the groups that failed and were left out (only
    with ``reduce_on_error="warn"``), by level (1 for the first reduce level) and group index; group ``i`` of
    a level held texts ``i * reduce_group_size`` to ``(i + 1) * reduce_group_size - 1`` of the level before
    (with ``balance_groups`` off).
    """

    output: str
    levels: list[list[str]]
    failures: ReduceFailures = field(default_factory=dict, repr=False)

    @property
    def depth(self) -> int:
        """How many reduce levels it took."""
        return len(self.levels) - 1

    @property
    def complete(self) -> bool:
        """True when no reduce group was left out, so the output covers every input."""
        return not self.failures


@dataclass(frozen=True)
class MapReduceResult:
    """What ``MapReduce.run`` returns: the DataFrame with the map outputs, and the reduced text."""

    frame: Any
    output: str
    levels: list[list[str]] = field(repr=False)
    map_failures: dict[int, LLMRequestError] = field(default_factory=dict, repr=False)
    reduce_failures: ReduceFailures = field(default_factory=dict, repr=False)

    @property
    def depth(self) -> int:
        return len(self.levels) - 1

    @property
    def complete(self) -> bool:
        """True when every record was mapped and no reduce group was left out."""
        return not self.map_failures and not self.reduce_failures


def reduce_levels(count: int, group_size: int) -> int:
    """How many reduce levels ``count`` inputs need to get down to one text (at least one)."""
    if not isinstance(group_size, int) or group_size < 2:
        raise ValueError(f"group_size must be 2 or more (got {group_size!r}).")
    levels = 1
    while count > group_size:
        count = math.ceil(count / group_size)
        levels += 1
    return levels


def make_groups(texts: Sequence[str], size: int, *, balanced: bool = False) -> list[list[str]]:
    """Split texts, in order, into ceil(n / size) groups of at most ``size``.

    Fixed groups fill up in turn (11 texts, size 10: 10 + 1); balanced groups differ by one at most (6 + 5).
    """
    if not balanced:
        return [list(texts[start : start + size]) for start in range(0, len(texts), size)]
    count = math.ceil(len(texts) / size)
    base, extra = divmod(len(texts), count)
    groups, start = [], 0
    for index in range(count):
        end = start + base + (1 if index < extra else 0)
        groups.append(list(texts[start:end]))
        start = end
    return groups


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
        map_batch_size: int = 1000,
        reduce_group_size: int = 10,
        reduce_batch_size: int | None = None,
        balance_groups: bool = False,
        strategies: Sequence[str] = STRATEGIES,
        max_concurrency: int = 16,
        batch_poll_interval: float = 60.0,
        batch_timeout: float | None = 24 * 3600.0,
        batch_cancel_wait: float = _CANCEL_GRACE_SECONDS,
        max_concurrent_batch_jobs: int = 4,
        batch_cleanup: bool = True,
        separator: str = "\n\n",
        placeholder: str = "{text}",
        on_error: OnError = "warn",
        reduce_on_error: OnError = "raise",
        show_progress: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(placeholder, str) or not placeholder:
            raise ConfigError(f"placeholder must be a non-empty string such as '{{text}}' (got {placeholder!r}).")
        for name, prompt in (("map_prompt", map_prompt), ("reduce_prompt", reduce_prompt)):
            _check_prompt(name, prompt, placeholder)
        if collapse_prompt is not None:
            _check_prompt("collapse_prompt", collapse_prompt, placeholder)
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ConfigError("system_prompt must be a string or None.")
        if not isinstance(separator, str):
            raise ConfigError(f"separator must be a string (got {separator!r}).")
        _check_positive("map_batch_size", map_batch_size)
        _check_positive("max_concurrency", max_concurrency)
        _check_positive("max_concurrent_batch_jobs", max_concurrent_batch_jobs)
        if reduce_batch_size is not None:
            _check_positive("reduce_batch_size", reduce_batch_size)
        if not isinstance(reduce_group_size, int) or isinstance(reduce_group_size, bool) or reduce_group_size < 2:
            raise ConfigError(
                f"reduce_group_size must be 2 or more, or the reduce never ends (got {reduce_group_size!r})."
            )
        _check_seconds("batch_poll_interval", batch_poll_interval)
        _check_seconds("batch_timeout", batch_timeout, allow_none=True)
        _check_seconds("batch_cancel_wait", batch_cancel_wait, allow_zero=True)
        for name, value in (("on_error", on_error), ("reduce_on_error", reduce_on_error)):
            if value not in ("warn", "raise"):
                raise ConfigError(f'{name} must be "warn" or "raise" (got {value!r}).')

        self.client = client
        self.map_prompt = map_prompt
        self.reduce_prompt = reduce_prompt
        self.collapse_prompt = collapse_prompt if collapse_prompt is not None else reduce_prompt
        self.system_prompt = system_prompt
        self.map_batch_size = map_batch_size
        self.reduce_group_size = reduce_group_size
        self.reduce_batch_size = reduce_batch_size or map_batch_size
        self.balance_groups = balance_groups
        self.separator = separator
        self.placeholder = placeholder
        self.on_error = on_error
        self.reduce_on_error = reduce_on_error
        self.show_progress = show_progress
        self._executor_options = {
            "strategies": tuple(strategies) if not isinstance(strategies, str) else strategies,
            "max_concurrency": max_concurrency,
            "batch_poll_interval": batch_poll_interval,
            "batch_timeout": batch_timeout,
            "batch_cancel_wait": batch_cancel_wait,
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
            frame.check(column, output_column, error_column)
            outputs, failures = self._map_frame(frame, column, output_column, error_column, self._new_executor())
            return frame.with_outputs(output_column, outputs, error_column=error_column, failures=failures)

    def map_texts(self, texts: Sequence[Any] | str) -> list[str | None]:
        """The map step on a plain list: one output per text (None for empty or failed ones)."""
        if is_pandas_dataframe(texts) or is_spark_dataframe(texts):
            raise TypeError("map_texts takes a list of texts; use map(df, column, output_column) for a DataFrame.")
        values = [texts] if isinstance(texts, str) else list(texts)
        with self._logging():
            outputs, _ = self._map(values, self._new_executor())
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
        """Map then reduce: the DataFrame with the map outputs, and the single reduced text.

        If the reduce fails, the exception carries the mapped DataFrame as ``frame``, so the map step
        isn't lost.
        """
        with self._logging():
            executor = self._new_executor()  # shared, so a strategy that broke in the map isn't retried
            frame = to_frame(data, id_column=id_column)
            frame.check(column, output_column, error_column)
            outputs, failures = self._map_frame(frame, column, output_column, error_column, executor)
            mapped = frame.with_outputs(output_column, outputs, error_column=error_column, failures=failures)
            try:
                reduced = self._reduce([text for text in outputs if text is not None], executor)
            except Exception as exc:
                attach(exc, frame=mapped, map_failures=failures)
                raise
            return MapReduceResult(
                frame=mapped,
                output=reduced.output,
                levels=reduced.levels,
                map_failures=failures,
                reduce_failures=reduced.failures,
            )

    # --- the two steps -----------------------------------------------------------------------------------

    def _map_frame(
        self, frame: Frame, column: str, output_column: str, error_column: str | None, executor: FallbackExecutor
    ) -> tuple[list[str | None], dict[int, LLMRequestError]]:
        """The map step on a DataFrame; if it stops, the exception also carries the partial DataFrame."""
        values = frame.values(column)
        try:
            return self._map(values, executor)
        except Exception as exc:
            outputs = getattr(exc, "outputs", None)
            if outputs is not None:
                with contextlib.suppress(Exception):
                    failures = getattr(exc, "failures", None)
                    attach(
                        exc,
                        frame=frame.with_outputs(output_column, outputs, error_column=error_column, failures=failures),
                    )
            raise

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
            try:
                results, failures = executor.run(requests, progress, batch_size=self.map_batch_size)
            except Exception as exc:
                partial = getattr(exc, "results", None) or {}
                attach(exc, outputs=[partial.get(index) for index in range(len(values))])
                raise
        outputs = [results.get(index) for index in range(len(values))]
        self._report_failures("map", "records", failures, outputs, len(requests), self.on_error)
        return outputs, failures

    def _reduce(self, texts: list[str], executor: FallbackExecutor) -> ReduceResult:
        if not texts:
            raise MapReduceError("Nothing to reduce: every input is empty (or failed in the map step).")
        size = self.reduce_group_size
        levels: list[list[str]] = [texts]
        all_failures: ReduceFailures = {}
        current = texts
        total_levels = reduce_levels(len(current), size)
        with levels_bar(total_levels, show=self.show_progress) as bar:
            level = 0
            while True:
                level += 1
                groups = make_groups(current, size, balanced=self.balance_groups)
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
                    try:
                        results, failures = executor.run(requests, progress, batch_size=self.reduce_batch_size)
                    except Exception as exc:
                        partial = getattr(exc, "results", None) or {}
                        attach(exc, outputs=[partial.get(index) for index in range(len(groups))], levels=levels)
                        raise
                outputs = [results.get(index) for index in range(len(groups))]
                try:
                    self._report_failures(
                        f"reduce level {level}", "groups", failures, outputs, len(groups), self.reduce_on_error
                    )
                except StepFailedError as exc:
                    attach(exc, levels=levels)
                    raise
                if failures:
                    all_failures[level] = dict(failures)
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
        return ReduceResult(output=current[0], levels=levels, failures=all_failures)

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

    @staticmethod
    def _report_failures(
        step: str,
        noun: str,
        failures: Mapping[int, LLMRequestError],
        outputs: list[str | None],
        attempted: int,
        on_error: OnError,
    ) -> None:
        if not failures:
            return
        reasons = Counter(str(error) for error in failures.values())
        top = "; ".join(f"{reason} (×{count})" for reason, count in reasons.most_common(3))
        message = f"{len(failures)} of {attempted} {noun} failed in the {step}: {top}"
        if on_error == "raise":
            raise StepFailedError(message, outputs=outputs, failures=dict(failures))
        if noun == "groups":
            message += ". The final text leaves out what those groups held."
        log.warning(message)

    @contextlib.contextmanager
    def _logging(self) -> Iterator[None]:
        """Keep log lines from breaking the progress bars."""
        if not self.show_progress:
            yield
            return
        with bar_safe_logging():
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


def _check_seconds(name: str, value: object, *, allow_none: bool = False, allow_zero: bool = False) -> None:
    if value is None and allow_none:
        return
    valid = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    if not valid or value < 0 or (value == 0 and not allow_zero):
        wanted = "zero or more" if allow_zero else "greater than zero"
        suffix = ", or None to wait as long as it takes" if allow_none else ""
        raise ConfigError(f"{name} must be a number of seconds {wanted}{suffix} (got {value!r}).")
