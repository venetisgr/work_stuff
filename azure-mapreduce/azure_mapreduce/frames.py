"""Reading a text column from, and adding the output column to, pandas and PySpark DataFrames.

Spark rows are processed on the driver: the Batch API needs every prompt in one input file, and async calls are
I/O-bound, so one process with many requests in flight is as fast as the deployment's quota allows. Only the
ids and texts are collected when you give an ``id_column``; otherwise the whole DataFrame is.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import ConfigError, LLMRequestError

log = logging.getLogger(__name__)

_JOIN_KEY = "__azure_mapreduce_id__"


def is_spark_dataframe(data: object) -> bool:
    """True for classic and Spark Connect DataFrames, without importing pyspark."""
    cls = type(data)
    return cls.__module__.startswith("pyspark.sql") and any(base.__name__ == "DataFrame" for base in cls.__mro__)


def is_pandas_dataframe(data: object) -> bool:
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is a dependency
        return False
    return isinstance(data, pd.DataFrame)


def as_text(value: object) -> str | None:
    """The text to send for one cell, or None to skip it (missing, NaN, blank or an empty list).

    Lists, arrays, dicts and Spark rows are sent as JSON, so nothing is cut short the way numpy's printout would.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, bytes | bytearray):
        text = bytes(value).decode("utf-8", "replace")
        return text if text.strip() else None
    if hasattr(value, "as_py"):  # a pyarrow scalar
        return as_text(value.as_py())
    if _is_container(value):
        plain = _plain(value)
        return json.dumps(plain, ensure_ascii=False, default=str) if plain else None
    if _is_missing(value):
        return None
    text = str(value)
    return text if text.strip() else None


def _is_container(value: object) -> bool:
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return True
    return hasattr(value, "asDict") or (hasattr(value, "tolist") and getattr(value, "ndim", 0) >= 1)


def _plain(value: Any) -> Any:
    """Plain Python values for json.dumps: Spark rows become dicts, numpy arrays and scalars become Python."""
    if hasattr(value, "asDict"):
        value = value.asDict(recursive=True)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist") and getattr(value, "ndim", None) is not None:
        return _plain(value.tolist()) if value.ndim else value.item()
    return value


def _is_missing(value: object) -> bool:
    if isinstance(value, float):
        return math.isnan(value)
    try:
        import pandas as pd

        missing = pd.isna(value)  # also numpy NaN/NaT scalars, pd.NA and pd.NaT
    except (ImportError, TypeError, ValueError):  # pragma: no cover
        return False
    return False if hasattr(missing, "__len__") else bool(missing)


class Frame:
    """A DataFrame the map step reads a column from and adds its output column to."""

    def check(self, column: str, output_column: str, error_column: str | None) -> None:
        """Reject bad column choices before any request is sent."""
        raise NotImplementedError

    def values(self, column: str) -> list[Any]:
        raise NotImplementedError

    def with_outputs(
        self,
        output_column: str,
        outputs: Sequence[str | None],
        *,
        error_column: str | None = None,
        failures: Mapping[int, LLMRequestError] | None = None,
    ) -> Any:
        raise NotImplementedError


class PandasFrame(Frame):
    def __init__(self, df: Any):
        self.df = df

    def check(self, column, output_column, error_column):
        columns = list(self.df.columns)
        _check_single_column(column, columns)
        _check_output_names(output_column, error_column)
        for name in (output_column, error_column):
            if name is not None and columns.count(name) > 1:
                raise ConfigError(f'The DataFrame has more than one column labelled "{name}"; rename them first.')

    def values(self, column: str) -> list[Any]:
        _check_single_column(column, list(self.df.columns))
        return self.df[column].tolist()

    def with_outputs(self, output_column, outputs, *, error_column=None, failures=None):
        """A copy of the DataFrame with the output column (and error column) added or replaced."""
        _check_output_names(output_column, error_column)
        frame = self.df.copy(deep=False)  # new columns only; the input DataFrame is left alone
        frame[output_column] = _object_series(self.df, outputs)
        if error_column:
            frame[error_column] = _object_series(self.df, _error_list(len(outputs), failures))
        return frame


class SparkFrame(Frame):
    def __init__(self, df: Any, *, id_column: str | None = None):
        self.df = df
        self.id_column = id_column
        self._table: Any = None  # the collected DataFrame as a pyarrow Table (PySpark 4+)
        self._rows: list[Any] | None = None  # or as Rows
        self._ids: list[Any] | None = None

    def check(self, column, output_column, error_column):
        _check_single_column(column, self.df.columns)
        if self.id_column is not None:
            _check_single_column(self.id_column, self.df.columns)
        self.check_names(output_column, error_column)

    def values(self, column: str) -> list[Any]:
        columns = self.df.columns
        _check_single_column(column, columns)
        if self.id_column is None:
            index = columns.index(column)
            table = self._collect_arrow()
            if table is not None:
                self._table = table
                return table.column(index).to_pylist()
            self._rows = self.df.collect()
            return [row[index] for row in self._rows]

        from pyspark.sql import functions as F

        _check_single_column(self.id_column, columns)
        pairs = self.df.select(F.col(_quoted(self.id_column)), F.col(_quoted(column))).collect()
        ids = [row[0] for row in pairs]
        if any(value is None or (isinstance(value, float) and math.isnan(value)) for value in ids):
            raise ConfigError(f'id_column "{self.id_column}" has empty or NaN values; every row needs an id.')
        try:
            unique = len(set(ids))
        except TypeError:
            raise ConfigError(
                f'id_column "{self.id_column}" must hold simple values such as numbers or strings.'
            ) from None
        if unique != len(ids):
            raise ConfigError(f'id_column "{self.id_column}" has repeated values; every row needs its own id.')
        self._ids = ids
        return [row[1] for row in pairs]

    def with_outputs(self, output_column, outputs, *, error_column=None, failures=None):
        """The DataFrame with the output column (and error column) added or replaced, at the end.

        Without an id_column the collected DataFrame is rebuilt with the new columns, keeping every column's
        type and the row order. With one, the replies are joined back on it.
        """
        from pyspark.sql.types import StringType, StructField, StructType

        self.check_names(output_column, error_column)
        errors = _error_list(len(outputs), failures)
        names = [output_column] + ([error_column] if error_column else [])
        new_fields = [StructField(name, StringType(), True) for name in names]
        new_values = [list(outputs)] + ([errors] if error_column else [])
        spark = self.df.sparkSession
        schema = self.df.schema
        keep = [i for i, name in enumerate(self.df.columns) if not any(self._same(name, new) for new in names)]
        fields = [schema.fields[i] for i in keep]

        if self.id_column is None:
            if self._table is not None:
                import pyarrow as pa

                table = self._table.select(keep)
                for name, values in zip(names, new_values, strict=True):
                    table = table.append_column(name, pa.array(values, type=pa.string()))
                return spark.createDataFrame(table, schema=StructType(fields + new_fields))
            if self._rows is None:
                raise RuntimeError("values() must be called before with_outputs().")
            data = [tuple(row[i] for i in keep) + tuple(v[n] for v in new_values) for n, row in enumerate(self._rows)]
            return spark.createDataFrame(data, StructType(fields + new_fields))

        if self._ids is None:
            raise RuntimeError("values() must be called before with_outputs().")
        id_type = schema[self.id_column].dataType
        replies = spark.createDataFrame(
            [(self._ids[n], *(v[n] for v in new_values)) for n in range(len(self._ids))],
            StructType([StructField(_JOIN_KEY, id_type, True), *new_fields]),
        )
        base = self.df.drop(*[self.df.columns[i] for i in range(len(self.df.columns)) if i not in keep])
        joined = base.join(replies, base[_quoted(self.id_column)] == replies[_JOIN_KEY], "left")
        return joined.drop(replies[_JOIN_KEY])

    def check_names(self, output_column: str, error_column: str | None) -> None:
        _check_output_names(output_column, error_column)
        if self.id_column is not None and (
            self._same(output_column, self.id_column) or self._same(error_column, self.id_column)
        ):
            raise ConfigError("The output and error columns can't replace the id_column.")
        if error_column is not None and self._same(error_column, output_column):
            raise ConfigError("The output and error columns need different names.")

    def _collect_arrow(self) -> Any:
        """The whole DataFrame as a pyarrow Table (PySpark 4+), which keeps timestamps exact; None if unavailable.

        Collecting Rows turns timestamps into local wall-clock times, which can't tell the two occurrences of
        the hour repeated when daylight saving time ends apart.
        """
        if not hasattr(self.df, "toArrow"):
            return None
        try:
            return self.df.toArrow()
        except Exception as exc:  # a type Arrow can't carry, or pyarrow missing: collect Rows instead
            log.debug("Collecting through Arrow failed (%s); collecting rows instead.", exc)
            return None

    def _same(self, left: str | None, right: str | None) -> bool:
        if left is None or right is None:
            return False
        if self._case_sensitive():
            return left == right
        return left.casefold() == right.casefold()

    def _case_sensitive(self) -> bool:
        try:
            return str(self.df.sparkSession.conf.get("spark.sql.caseSensitive", "false")).lower() == "true"
        except Exception:  # pragma: no cover - a session that can't report its settings
            return False


def to_frame(data: Any, *, id_column: str | None = None) -> Frame:
    if is_spark_dataframe(data):
        return SparkFrame(data, id_column=id_column)
    if is_pandas_dataframe(data):
        if id_column is not None:
            raise ConfigError("id_column is for Spark DataFrames; pandas results line up by index.")
        return PandasFrame(data)
    raise TypeError(f"Expected a pandas or PySpark DataFrame, got {type(data).__name__}.")


def column_values(data: Any, column: str | None) -> list[Any]:
    """The values to reduce: a DataFrame column, a pandas Series, or any iterable of strings."""
    if is_spark_dataframe(data):
        if column is None:
            raise ConfigError("Say which column of the Spark DataFrame to reduce.")
        from pyspark.sql import functions as F

        _check_single_column(column, data.columns)
        return [row[0] for row in data.select(F.col(_quoted(column))).collect()]
    if is_pandas_dataframe(data):
        if column is None:
            raise ConfigError("Say which column of the pandas DataFrame to reduce.")
        _check_single_column(column, list(data.columns))
        return data[column].tolist()
    if column is not None:
        raise ConfigError("column only applies when reducing a DataFrame.")
    if isinstance(data, str):
        return [data]
    if type(data).__module__.startswith("pyspark"):
        raise TypeError(f"Pass a Spark DataFrame and a column name, not a {type(data).__name__}.")
    if hasattr(data, "tolist"):  # a pandas Series or numpy array
        return list(data.tolist())
    if isinstance(data, Iterable):
        return list(data)
    raise TypeError(f"Expected a DataFrame, a Series or a list of strings, got {type(data).__name__}.")


def _check_single_column(column: str, columns: Sequence[Any]) -> None:
    if column not in columns:
        raise ConfigError(f'Column "{column}" not found. The DataFrame has: {", ".join(map(str, columns))}')
    if list(columns).count(column) > 1:
        raise ConfigError(f'The DataFrame has more than one column named "{column}"; rename or alias them first.')


def _check_output_names(output_column: object, error_column: object) -> None:
    if not isinstance(output_column, str) or not output_column:
        raise ConfigError(f"output_column must be a column name (got {output_column!r}).")
    if error_column is not None and (not isinstance(error_column, str) or not error_column):
        raise ConfigError(f"error_column must be a column name or None (got {error_column!r}).")
    if error_column == output_column:
        raise ConfigError("The output and error columns need different names.")


def _quoted(name: str) -> str:
    """A column name Spark takes literally (dots and all)."""
    return "`" + name.replace("`", "``") + "`"


def _error_list(size: int, failures: Mapping[int, LLMRequestError] | None) -> list[str | None]:
    failures = failures or {}
    return [str(failures[i]) if i in failures else None for i in range(size)]


def _object_series(df: Any, values: Sequence[str | None]) -> Any:
    import pandas as pd

    return pd.Series(list(values), index=df.index, dtype="object")
