"""Reading a text column from, and adding the output column to, pandas and PySpark DataFrames.

Spark rows are processed on the driver: the Batch API needs every prompt in one input file, and async calls are
I/O-bound, so one process with many requests in flight is as fast as the deployment's quota allows. Only the
ids and texts are collected when you give an ``id_column``; otherwise the whole DataFrame is.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

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
        return {_key(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    kind = getattr(getattr(value, "dtype", None), "kind", None)
    if kind in ("M", "m"):  # numpy dates and durations, item by item: .tolist() turns nanosecond ones into integers
        if getattr(value, "ndim", 0):
            return [_plain(item) for item in value]
        if kind == "m" and value == value:  # a duration (not NaT) reads best as a Python timedelta: "1:00:00"
            return str(value.astype("timedelta64[us]").item())
        return str(value)
    if hasattr(value, "tolist") and getattr(value, "ndim", None) is not None:
        return _plain(value.tolist()) if value.ndim else value.item()
    return value


def _key(key: object) -> str:
    return bytes(key).decode("utf-8", "replace") if isinstance(key, bytes | bytearray) else str(key)


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
        self._id_array: Any = None  # the ids as collected through Arrow, exact

    def check(self, column, output_column, error_column):
        _check_single_column(column, self.df.columns)
        if self.id_column is not None:
            _check_single_column(self.id_column, self.df.columns)
            self._check_id_type(self.df.schema[self.id_column].dataType)
        self.check_names(output_column, error_column)

    def _check_id_type(self, data_type: Any) -> None:
        """Reject id types the replies can't be joined back on exactly, before any request is sent."""
        unjoinable = _type_names(data_type) & {"MapType", "VariantType", "UserDefinedType", *_SPATIAL_TYPES}
        if unjoinable:
            raise ConfigError(
                f'id_column "{self.id_column}" is of a type Spark can\'t join on ({", ".join(sorted(unjoinable))}); '
                "use a column of numbers or strings."
            )
        collations = _collations(data_type) - {"UTF8_BINARY"}
        if collations:
            raise ConfigError(
                f'id_column "{self.id_column}" compares strings with the {", ".join(sorted(collations))} collation, '
                "so ids that differ (in case, say) could match each other; cast it to a plain string first."
            )

    def values(self, column: str) -> list[Any]:
        columns = self.df.columns
        _check_single_column(column, columns)
        if self.id_column is None:
            index = columns.index(column)
            text_type = self.df.schema.fields[index].dataType
            self._table = _collect_arrow(self.df)
            if self._table is not None:
                return self._session_times(_arrow_list(self._table.column(index)), text_type)
            self._rows = _collect_rows(self.df)
            return self._session_times([row[index] for row in self._rows], text_type)

        from pyspark.sql import functions as F

        _check_single_column(self.id_column, columns)
        self._check_id_type(self.df.schema[self.id_column].dataType)
        pairs = self.df.select(F.col(_quoted(self.id_column)), F.col(_quoted(column)))
        text_type = pairs.schema.fields[1].dataType
        table = _collect_arrow(pairs)
        if table is not None:
            ids, texts = table.column(0).to_pylist(), _arrow_list(table.column(1))
        else:
            rows = _collect_rows(pairs)
            ids, texts = [row[0] for row in rows], [row[1] for row in rows]
        texts = self._session_times(texts, text_type)
        self._check_ids(ids)
        self._ids = ids
        self._id_array = table.column(0) if table is not None else None
        return texts

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
        reply_schema = StructType([StructField(_JOIN_KEY, schema[self.id_column].dataType, True), *new_fields])
        if self._id_array is not None:  # the exact ids, so timestamps in a repeated DST hour still match
            import pyarrow as pa

            columns = [self._id_array] + [pa.chunked_array([pa.array(v, type=pa.string())]) for v in new_values]
            table = pa.Table.from_arrays(columns, names=[_JOIN_KEY, *names])
            replies = spark.createDataFrame(table, schema=reply_schema)
        else:
            replies = spark.createDataFrame(
                [(self._ids[n], *(v[n] for v in new_values)) for n in range(len(self._ids))], reply_schema
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

    def _check_ids(self, ids: list[Any]) -> None:
        if any(value is None or _has_nan(value) for value in ids):
            raise ConfigError(f'id_column "{self.id_column}" has empty or NaN values; every row needs an id.')
        try:
            unique = len({_frozen(value) for value in ids})
        except TypeError:
            raise ConfigError(
                f'id_column "{self.id_column}" must hold simple values such as numbers or strings.'
            ) from None
        if unique != len(ids):
            raise ConfigError(f'id_column "{self.id_column}" has repeated values; every row needs its own id.')

    def _session_times(self, values: list[Any], data_type: Any) -> list[Any]:
        """Timestamps as wall-clock times in the session time zone, what df.show() prints, whichever way the
        data was collected (Arrow gives UTC, Rows give the driver's local time)."""
        if "TimestampType" not in _type_names(data_type):
            return values
        zone = _session_zone(self.df)
        return [_wall_clock(value, data_type, zone) for value in values]

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


# Types a DataFrame can't make the round trip through Arrow with (or that Arrow hands back as raw encodings),
# so frames holding them are collected as Rows instead.
_NOT_THROUGH_ARROW = {
    "UserDefinedType",
    "VariantType",
    "GeometryType",
    "GeographyType",
    "YearMonthIntervalType",
    "CalendarIntervalType",
    "TimeType",
    "NullType",
}


_SPATIAL_TYPES = {"GeometryType", "GeographyType"}


def _arrow_list(array: Any) -> list[Any]:
    """An Arrow column as Python values the way collect() gives them (maps as dicts, not key-value pairs)."""
    try:
        return array.to_pylist(maps_as_pydicts="lossy")
    except TypeError:  # pyarrow before 13
        return array.to_pylist()


def _collect_rows(df: Any) -> list[Any]:
    try:
        return df.collect()
    except NotImplementedError as exc:  # a type PySpark can't turn into Python values
        raise ConfigError(
            f"Spark can't hand this DataFrame's values to Python ({exc}). Cast or drop the columns of that type, "
            "or pass id_column so that only the ids and texts are collected."
        ) from exc


def _collect_arrow(df: Any) -> Any:
    """The DataFrame as a pyarrow Table (PySpark 4+), which keeps timestamps exact; None if it can't be.

    Collecting Rows turns timestamps into local wall-clock times, which can't tell the two occurrences of the
    hour repeated when daylight saving time ends apart.
    """
    if not hasattr(df, "toArrow") or not _arrow_safe(df.schema):
        return None
    try:
        return df.toArrow()
    except _arrow_conversion_errors() as exc:  # a conversion problem, not the query failing
        log.debug("Collecting through Arrow failed (%s); collecting rows instead.", exc)
        return None


def _arrow_conversion_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (ImportError, TypeError, ValueError, NotImplementedError)
    try:  # raised while the schema is converted, before the query runs
        from pyspark.errors.exceptions.base import UnsupportedOperationException
    except ImportError:  # pragma: no cover - older PySpark
        return errors
    return (*errors, UnsupportedOperationException)


def _arrow_safe(data_type: Any) -> bool:
    return not (_type_names(data_type) & _NOT_THROUGH_ARROW) and not _repeated_field_names(data_type)


def _repeated_field_names(data_type: Any) -> bool:
    """True if a nested struct has two fields of the same name (Arrow refuses those; a join can make them)."""
    fields = getattr(data_type, "fields", None) or []
    names = [field.name for field in fields]
    if isinstance(data_type, _struct_class()) and len(names) != len(set(names)):
        return True
    nested = [field.dataType for field in fields]
    nested += [getattr(data_type, name) for name in ("elementType", "keyType", "valueType") if hasattr(data_type, name)]
    return any(_repeated_field_names(item) for item in nested)


def _struct_class() -> type:
    from pyspark.sql.types import StructType

    return StructType


def _collations(data_type: Any) -> set[str]:
    found = {data_type.collation} if isinstance(getattr(data_type, "collation", None), str) else set()
    for field in getattr(data_type, "fields", None) or []:
        found |= _collations(field.dataType)
    for name in ("elementType", "keyType", "valueType"):
        if hasattr(data_type, name):
            found |= _collations(getattr(data_type, name))
    return found


def _type_names(data_type: Any) -> set[str]:
    """The class names of a Spark type and every type nested in it (a UDT also counts as UserDefinedType)."""
    names = {cls.__name__ for cls in type(data_type).__mro__}
    for field in getattr(data_type, "fields", None) or []:
        names |= _type_names(field.dataType)
    for attribute in ("elementType", "keyType", "valueType"):
        nested = getattr(data_type, attribute, None)
        if nested is not None:
            names |= _type_names(nested)
    return names


_OFFSET = re.compile(r"^(?:GMT|UTC|UT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _session_zone(df: Any) -> tzinfo:
    """spark.sql.session.timeZone as a tzinfo: a region ("Europe/Paris") or an offset ("+01:00", "GMT+05:30")."""
    try:
        name = str(df.sparkSession.conf.get("spark.sql.session.timeZone")).strip()
    except Exception:  # pragma: no cover - a session that can't report its settings
        return UTC
    match = _OFFSET.match(name)
    if match:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return timezone(-offset if sign == "-" else offset)
    try:
        return ZoneInfo(name)
    except Exception:  # an unknown name
        log.debug("Unknown session time zone %r; showing timestamps in UTC.", name)
        return UTC


def _wall_clock(value: Any, data_type: Any, zone: tzinfo) -> Any:
    """Convert the TIMESTAMP values inside a value, following its Spark type (TIMESTAMP_NTZ is left alone)."""
    if value is None:
        return None
    type_name = type(data_type).__name__
    if type_name == "TimestampType":
        if not isinstance(value, datetime):
            return value
        aware = value if value.tzinfo is not None else value.astimezone()  # Rows are in the driver's zone
        return aware.astimezone(zone).replace(tzinfo=None)
    fields = getattr(data_type, "fields", None)
    if fields is not None and type_name == "StructType":
        items = value.asDict() if hasattr(value, "asDict") else value
        if isinstance(items, Mapping):
            return {field.name: _wall_clock(items.get(field.name), field.dataType, zone) for field in fields}
        return value
    if type_name == "ArrayType" and isinstance(value, list | tuple):
        return [_wall_clock(item, data_type.elementType, zone) for item in value]
    if type_name == "MapType" and isinstance(value, Mapping):
        return {
            _wall_clock(key, data_type.keyType, zone): _wall_clock(item, data_type.valueType, zone)
            for key, item in value.items()
        }
    return value


def _has_nan(value: Any) -> bool:
    if isinstance(value, float):
        return math.isnan(value)
    if hasattr(value, "asDict"):
        value = value.asDict(recursive=True)
    if isinstance(value, Mapping):
        return any(_has_nan(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_has_nan(item) for item in value)
    return False


def _frozen(value: Any) -> Any:
    """A hashable stand-in for an id (Arrow hands structs back as dicts and arrays as lists; Rows are tuples)."""
    if isinstance(value, Mapping):
        return tuple((key, _frozen(item)) for key, item in value.items())
    if isinstance(value, list | tuple):
        return tuple(_frozen(item) for item in value)
    return value


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
    if isinstance(data, str | bytes | bytearray):
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
