"""The exceptions the framework raises, and how it tells one bad record from a broken setup."""

from __future__ import annotations

import contextlib


class MapReduceError(Exception):
    """Base class for everything this package raises on purpose."""


class ConfigError(MapReduceError):
    """A setting is missing or wrong. The message says what to fix."""


class LLMSetupError(MapReduceError):
    """Every request would fail the same way (credentials, endpoint, deployment), so there's no point going on."""


class LLMRequestError(MapReduceError):
    """One request failed.

    ``retryable`` says whether another strategy could still succeed: throttling or a timeout might clear up,
    but a content-filter block or an over-long input fails the same way however the request is sent.
    """

    def __init__(self, message: str, *, retryable: bool = True, code: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class StepFailedError(MapReduceError):
    """Some records or groups failed with ``on_error="raise"`` (or ``reduce_on_error="raise"``).

    ``outputs`` holds everything that did succeed (None where a record or group failed), so the work that
    was paid for isn't lost; ``failures`` maps each failed position to its error.

    Any other exception that stops a step (an ``LLMSetupError``, say) gets the same ``outputs`` attribute, and
    one that stops ``MapReduce.run`` during the reduce also gets ``frame``: the DataFrame from the map step.
    """

    def __init__(self, message: str, *, outputs: list[str | None], failures: dict[int, LLMRequestError]):
        super().__init__(message)
        self.outputs = outputs
        self.failures = failures


def attach(exc: BaseException, **values: object) -> None:
    """Hang partial results on an exception on its way out, so the work that was paid for isn't lost."""
    for name, value in values.items():
        with contextlib.suppress(AttributeError, TypeError):  # exceptions without a __dict__
            setattr(exc, name, value)
