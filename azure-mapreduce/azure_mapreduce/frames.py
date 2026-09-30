"""Reading a text column from, and adding the output column to, pandas and PySpark DataFrames.

Spark rows are processed on the driver: the Batch API needs every prompt in one input file, and async calls are
I/O-bound, so one process with many requests in flight is as fast as the deployment's quota allows. Only the
columns needed are collected when you give an ``id_column``; otherwise the whole DataFrame is.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import ConfigError, LLMRequestError


def is_spark_dataframe(data: object) -> bool:
    """True for classic and Spark Connect DataFrames, without importing pyspark."""
    return type(data).__module__.startswith("pyspark.sql") and hasattr(data, "sparkSession")


def is_pandas_dataframe(data: object) -> bool:
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is a dependency
        return False
    return isinstance(data, pd.DataFrame)


def as_text(value: object) -> str | None:
    """The text to send for one cell, or None to skip it (missing, NaN or blank)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, bytes | bytearray):
        text = bytes(value).decode("utf-8", "replace")
        return text if text.strip() else None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
    except ImportError:  # pragma: no cover
        pass
    text = str(value)
    return text if text.strip() else None


class Frame:
    """A DataFrame the map step reads a column from and adds its output column to."""

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

    def values(self, column: str) -> list[Any]:
        _check_column(column, list(self.df.columns))
        return self.df[column].tolist()

    def with_outputs(self, output_column, outputs, *, error_column=None, failures=None):
        """A copy of the DataFrame with the output column (and error column) added or replaced."""
        columns = {output_column: _object_series(self.df, outputs)}
        if error_column:
            errors = _error_list(len(outputs), failures)
            columns[error_column] = _object_series(self.df, errors)
        return self.df.assign(**columns)


class SparkFrame(Frame):
    def __init__(self, df: Any, *, id_column: str | None = None):
        self.df = df
        self.id_column = id_column
        self._rows: list[Any] | None = None
        self._ids: list[Any] | None = None

    def values(self, column: str) -> list[Any]:
        _check_column(column, self.df.columns)
        if self.id_column is None:
            self._rows = self.df.collect()
            index = self.df.columns.index(column)
            return [row[index] for row in self._rows]

        from pyspark.sql import functions as F

        _check_column(self.id_column, self.df.columns)
        pairs = self.df.select(F.col(self.id_column).alias("id"), F.col(column).alias("text")).collect()
        ids = [row[0] for row in pairs]
        if any(value is None for value in ids):
            raise ConfigError(f'id_column "{self.id_column}" has empty values; every row needs an id.')
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
        """The DataFrame with the output column (and error column) added or replaced.

        Without an id_column the rows collected by values() are rebuilt with the new columns at the end,
        keeping every column's type. With one, the replies are joined back on it (row order isn't kept,
        as usual for a Spark join).
        """
        from pyspark.sql.types import StringType, StructField, StructType

        if output_column == self.id_column or (error_column and error_column == self.id_column):
            raise ConfigError("The output and error columns can't replace the id_column.")
        if error_column and error_column == output_column:
            raise ConfigError("The output and error columns need different names.")
        errors = _error_list(len(outputs), failures)
        new_fields = [StructField(output_column, StringType(), True)]
        if error_column:
            new_fields.append(StructField(error_column, StringType(), True))

        def extra(i: int) -> tuple[Any, ...]:
            return (outputs[i], errors[i]) if error_column else (outputs[i],)

        spark = self.df.sparkSession
        replaced = {output_column, error_column} - {None}
        if self.id_column is None:
            if self._rows is None:
                raise RuntimeError("values() must be called before with_outputs().")
            keep = [i for i, name in enumerate(self.df.columns) if name not in replaced]
            fields = [self.df.schema.fields[i] for i in keep]
            data = [tuple(row[i] for i in keep) + extra(n) for n, row in enumerate(self._rows)]
            return spark.createDataFrame(data, StructType(fields + new_fields))

        if self._ids is None:
            raise RuntimeError("values() must be called before with_outputs().")
        id_field = self.df.schema[self.id_column]
        replies = spark.createDataFrame(
            [(self._ids[n], *extra(n)) for n in range(len(self._ids))],
            StructType([StructField(self.id_column, id_field.dataType, True), *new_fields]),
        )
        base = self.df.drop(*[name for name in replaced if name in self.df.columns])
        return base.join(replies, on=self.id_column, how="left")


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
        _check_column(column, data.columns)
        return [row[0] for row in data.select(column).collect()]
    if is_pandas_dataframe(data):
        if column is None:
            raise ConfigError("Say which column of the pandas DataFrame to reduce.")
        _check_column(column, list(data.columns))
        return data[column].tolist()
    if isinstance(data, str):
        return [data]
    if column is not None:
        raise ConfigError("column only applies when reducing a DataFrame.")
    if hasattr(data, "tolist"):  # a pandas Series or numpy array
        return list(data.tolist())
    if isinstance(data, Iterable):
        return list(data)
    raise TypeError(f"Expected a DataFrame, a Series or a list of strings, got {type(data).__name__}.")


def _check_column(column: str, columns: Sequence[str]) -> None:
    if column not in columns:
        raise ConfigError(f'Column "{column}" not found. The DataFrame has: {", ".join(map(str, columns))}')


def _error_list(size: int, failures: Mapping[int, LLMRequestError] | None) -> list[str | None]:
    failures = failures or {}
    return [str(failures[i]) if i in failures else None for i in range(size)]


def _object_series(df: Any, values: Sequence[str | None]) -> Any:
    import pandas as pd

    return pd.Series(list(values), index=df.index, dtype="object")
