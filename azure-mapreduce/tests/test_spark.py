"""PySpark support: SparkFrame, column_values and MapReduce.map/run/reduce on Spark DataFrames.

One small local Spark session serves the whole module. Most tests use the loop strategy since the Spark adapter
doesn't care how the replies were made; the end-to-end runs go through the (fake) Batch API as well.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

pytest.importorskip("pyspark")

from pyspark.sql import Row, SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql import types as T  # noqa: E402
from pyspark.sql.types import (  # noqa: E402
    ArrayType,
    BinaryType,
    BooleanType,
    ByteType,
    DateType,
    DayTimeIntervalType,
    DecimalType,
    DoubleType,
    FloatType,
    IntegerType,
    LongType,
    MapType,
    ShortType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from azure_mapreduce import MapReduce, MapReduceResult, ReduceResult  # noqa: E402
from azure_mapreduce.errors import (  # noqa: E402
    ConfigError,
    LLMRequestError,
    LLMSetupError,
    MapReduceError,
    StepFailedError,
)
from azure_mapreduce.frames import SparkFrame, column_values, is_spark_dataframe, to_frame  # noqa: E402
from azure_mapreduce.runners import STRATEGIES  # noqa: E402

from .conftest import FakeClient, content_of, echo  # noqa: E402

# --- helpers ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spark():
    try:
        session = (
            SparkSession.builder.master("local[1]")
            .appName("azure-mapreduce-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "1")
            .getOrCreate()
        )
    except Exception as exc:  # pragma: no cover - no Java on this machine
        pytest.skip(f"A local Spark session can't start here: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def make(client, clock, **options) -> MapReduce:
    options.setdefault("strategies", ("sync",))
    return MapReduce(
        client,
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        show_progress=options.pop("show_progress", False),
        batch_poll_interval=1.0,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )


def summarized(text: str | None) -> str | None:
    """What the map step should put in the output column for this cell with the default echo responder."""
    return f"<Summarize: {text}>" if text is not None and text.strip() else None


def no_calls(client: FakeClient) -> bool:
    return not any(client.calls.values()) and not client.jobs


def failing_on(marker: str, message: str = "content filtered"):
    """A responder that fails (for good) every prompt containing ``marker`` and echoes the rest."""

    def responder(messages):
        if marker in content_of(messages):
            raise LLMRequestError(message, retryable=False, code="content_filter")
        return echo(messages)

    return responder


STRING = StructField("summary", StringType(), True)

INFO = StructType([StructField("a", IntegerType(), True), StructField("when", TimestampType(), True)])
TYPED_SCHEMA = StructType(
    [
        StructField("n", IntegerType(), False),
        StructField("big", LongType(), True),
        StructField("text", StringType(), True),
        StructField("ts", TimestampType(), True),
        StructField("day", DateType(), True),
        StructField("price", DecimalType(12, 4), True),
        StructField("score", DoubleType(), True),
        StructField("flag", BooleanType(), True),
        StructField("tags", ArrayType(StringType(), True), True),
        StructField("info", INFO, True),
        StructField("attrs", MapType(StringType(), IntegerType(), True), True),
        StructField("raw", BinaryType(), True),
    ]
)
TYPED_ROWS = [
    (
        5,
        2**40,
        "first",
        datetime(2024, 1, 2, 3, 4, 5, 678901),
        date(2024, 1, 2),
        Decimal("12.3400"),
        1.5,
        True,
        ["x", "y"],
        Row(a=1, when=datetime(2023, 12, 31, 23, 59, 59)),
        {"k": 1},
        bytearray(b"\x00\x01"),
    ),
    (3, None, None, None, None, None, None, None, None, None, None, None),
    (
        9,
        -1,
        "   ",
        datetime(1999, 12, 31),
        date(1970, 1, 1),
        Decimal("-0.0001"),
        -2.0,
        False,
        [],
        Row(a=None, when=None),
        {},
        bytearray(),
    ),
    (
        1,
        0,
        "",
        datetime(2030, 6, 15, 12),
        date(2030, 6, 15),
        Decimal("0.0000"),
        0.0,
        None,
        [None, "z"],
        None,
        {"n": None},
        None,
    ),
    (
        7,
        7,
        "last",
        datetime(2024, 2, 29),
        date(2024, 2, 29),
        Decimal("99999999.9999"),
        1e300,
        True,
        ["q"],
        Row(a=7, when=None),
        {"a": 1, "b": 2},
        bytearray(b"\xff"),
    ),
]


@pytest.fixture
def typed_df(spark):
    return spark.createDataFrame(TYPED_ROWS, TYPED_SCHEMA)


def simple_df(spark, rows, schema="id long, text string"):
    return spark.createDataFrame(rows, schema)


def by_id(frame, *columns: str) -> dict:
    """The rows of a Spark DataFrame keyed by id, as tuples of the given columns."""
    rows = frame.collect()
    assert len(rows) == len({row["id"] for row in rows}), "the result has repeated ids"
    return {row["id"]: tuple(row[name] for name in columns) for row in rows}


# --- is_spark_dataframe / to_frame / column_values -----------------------------------------------------


def test_is_spark_dataframe_recognises_spark_dataframes(spark):
    df = simple_df(spark, [(1, "a")])
    assert is_spark_dataframe(df)
    assert is_spark_dataframe(df.select("text"))
    assert not is_spark_dataframe(df.collect())
    assert not is_spark_dataframe(df.collect()[0])
    assert not is_spark_dataframe(df.groupBy("id"))
    assert not is_spark_dataframe(pd.DataFrame({"text": ["a"]}))


def test_is_spark_dataframe_rejects_a_spark_column(spark):
    df = simple_df(spark, [(1, "a")])
    assert not is_spark_dataframe(df.text)
    assert not is_spark_dataframe(F.col("text"))


def test_to_frame_wraps_spark_dataframes(spark):
    df = simple_df(spark, [(1, "a")])
    frame = to_frame(df, id_column="id")
    assert isinstance(frame, SparkFrame)
    assert frame.df is df
    assert frame.id_column == "id"
    assert isinstance(to_frame(df), SparkFrame)


def test_column_values_reads_a_spark_column_in_order(spark):
    df = simple_df(spark, [(3, "c"), (1, None), (2, "  "), (4, "a")])
    assert column_values(df, "text") == ["c", None, "  ", "a"]
    assert column_values(df, "id") == [3, 1, 2, 4]


def test_column_values_needs_a_column_for_spark(spark):
    df = simple_df(spark, [(1, "a")])
    with pytest.raises(ConfigError, match="Say which column"):
        column_values(df, None)
    with pytest.raises(ConfigError, match='Column "nope" not found. The DataFrame has: id, text'):
        column_values(df, "nope")


def test_spark_frame_needs_values_before_with_outputs(spark):
    df = simple_df(spark, [(1, "a")])
    with pytest.raises(RuntimeError, match="values"):
        SparkFrame(df).with_outputs("summary", ["x"])
    with pytest.raises(RuntimeError, match="values"):
        SparkFrame(df, id_column="id").with_outputs("summary", ["x"])


# --- map without id_column: collect the rows and rebuild them ------------------------------------------


def test_map_keeps_every_column_its_type_and_the_row_order(typed_df, clock):
    client = FakeClient()
    out = make(client, clock).map(typed_df, "text", "summary")

    assert is_spark_dataframe(out)
    assert out.columns == [*typed_df.columns, "summary"]
    assert out.schema.fields[:-1] == typed_df.schema.fields  # names, types and nullability
    assert out.schema.fields[-1] == STRING
    rows = out.collect()
    assert [tuple(row)[:-1] for row in rows] == [tuple(row) for row in typed_df.collect()]
    assert [row["n"] for row in rows] == [5, 3, 9, 1, 7]  # not sorted by anything
    assert [row["summary"] for row in rows] == ["<Summarize: first>", None, None, None, "<Summarize: last>"]
    assert [content_of(m) for m in client.calls["sync"]] == ["Summarize: first", "Summarize: last"]


def test_map_skips_null_and_blank_cells(spark, clock):
    texts = [None, "", "   ", "\n\t", "keep me", " padded "]
    df = simple_df(spark, list(enumerate(texts)))
    client = FakeClient()
    out = make(client, clock).map(df, "text", "summary")
    assert [row["summary"] for row in out.collect()] == [summarized(text) for text in texts]
    assert [content_of(m) for m in client.calls["sync"]] == ["Summarize: keep me", "Summarize:  padded "]


def test_map_keeps_the_order_of_a_multi_partition_dataframe(spark, clock):
    df = spark.range(0, 60, numPartitions=4).select(
        F.col("id"), F.concat(F.lit("t"), (59 - F.col("id")).cast("string")).alias("text")
    )
    out = make(FakeClient(), clock, map_batch_size=7).map(df, "text", "summary")
    rows = out.collect()
    assert [row["id"] for row in rows] == list(range(60))
    assert [row["summary"] for row in rows] == [f"<Summarize: t{59 - i}>" for i in range(60)]
    assert out.count() == 60


def test_map_sends_non_string_cells_as_text(spark, clock):
    df = spark.createDataFrame([(1, 2.5), (2, None), (3, -7.0)], "id long, value double")
    client = FakeClient()
    out = make(client, clock).map(df, "value", "summary")
    assert [row["summary"] for row in out.collect()] == ["<Summarize: 2.5>", None, "<Summarize: -7.0>"]
    assert out.schema["value"].dataType == DoubleType()


def test_map_all_blank_column_still_adds_a_string_column(spark, clock):
    df = simple_df(spark, [(1, None), (2, " "), (3, "")])
    client = FakeClient()
    out = make(client, clock).map(df, "text", "summary")
    assert out.schema.fields[-1] == STRING
    assert [row["summary"] for row in out.collect()] == [None, None, None]
    assert no_calls(client)


def test_map_leaves_the_input_dataframe_alone(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    before = (df.columns, df.schema, df.collect())
    make(FakeClient(), clock).map(df, "text", "summary")
    assert (df.columns, df.schema, df.collect()) == before


def test_map_handles_column_names_with_dots_and_spaces_without_id_column(spark, clock):
    df = spark.createDataFrame([(1, "a"), (2, None)], "`row id` long, `review.text` string")
    out = make(FakeClient(), clock).map(df, "review.text", "the summary")
    assert out.columns == ["row id", "review.text", "the summary"]
    assert [row[2] for row in out.collect()] == ["<Summarize: a>", None]


@pytest.fixture
def new_york_time(monkeypatch):
    """Run the Python side (where Spark converts timestamps) in a time zone with daylight saving."""
    if not hasattr(time, "tzset"):  # pragma: no cover - Windows
        pytest.skip("needs time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("id_column", [None, "id"])
def test_map_keeps_timestamps_in_the_repeated_daylight_saving_hour(spark, clock, new_york_time, id_column):
    """2024-11-03 01:30 happens twice in New York (05:30 and 06:30 UTC); both instants must survive."""
    df = spark.sql(
        "SELECT s AS id, timestamp_seconds(s) AS ts, CAST(s AS STRING) AS text "
        "FROM VALUES (1730611800L), (1730615400L) AS t(s)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column=id_column)
    rows = out.select("id", F.col("ts").cast("long"), "summary").collect()
    assert sorted(tuple(row) for row in rows) == [
        (1730611800, 1730611800, "<Summarize: 1730611800>"),
        (1730615400, 1730615400, "<Summarize: 1730615400>"),
    ]
    assert out.schema["ts"].dataType == TimestampType()


# --- output_column and error_column --------------------------------------------------------------------


@pytest.mark.parametrize("id_column", [None, "id"])
def test_output_column_replaces_an_existing_column(spark, clock, id_column):
    df = spark.createDataFrame(
        [(1, "a", 10, True), (2, None, 20, False)], "id long, text string, summary int, flag boolean"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column=id_column)
    assert out.columns.count("summary") == 1
    assert [name for name in out.columns if name != "summary"] == ["id", "text", "flag"]
    assert out.schema["summary"] == STRING
    assert by_id(out, "text", "summary", "flag") == {1: ("a", "<Summarize: a>", True), 2: (None, None, False)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_output_column_can_replace_the_text_column(spark, clock, id_column):
    df = simple_df(spark, [(1, "a"), (2, None), (3, "c")])
    out = make(FakeClient(), clock).map(df, "text", "text", id_column=id_column)
    assert sorted(out.columns) == ["id", "text"]
    assert out.schema["text"] == StructField("text", StringType(), True)
    assert by_id(out, "text") == {1: ("<Summarize: a>",), 2: (None,), 3: ("<Summarize: c>",)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_output_column_differing_only_in_case_replaces_the_column(spark, clock, id_column):
    df = spark.createDataFrame([(1, "a", 10), (2, "b", 20)], "id long, text string, summary int")
    out = make(FakeClient(), clock).map(df, "text", "Summary", id_column=id_column)
    assert out.columns == ["id", "text", "Summary"]
    assert out.schema["Summary"] == StructField("Summary", StringType(), True)
    assert by_id(out, "Summary") == {1: ("<Summarize: a>",), 2: ("<Summarize: b>",)}
    assert [row[0] for row in out.orderBy("id").select("summary").collect()] == ["<Summarize: a>", "<Summarize: b>"]


@pytest.mark.parametrize("id_column", [None, "id"])
def test_error_column_holds_each_failed_records_error(spark, clock, id_column):
    df = simple_df(spark, [(1, "fine"), (2, "bad one"), (3, None), (4, "also bad"), (5, "ok")])
    client = FakeClient(responder=failing_on("bad"))
    out = make(client, clock).map(df, "text", "summary", error_column="error", id_column=id_column)

    assert out.columns[-2:] == ["summary", "error"]
    assert out.schema["error"] == StructField("error", StringType(), True)
    assert by_id(out, "summary", "error") == {
        1: ("<Summarize: fine>", None),
        2: (None, "content filtered"),
        3: (None, None),  # skipped, not failed
        4: (None, "content filtered"),
        5: ("<Summarize: ok>", None),
    }


@pytest.mark.parametrize("id_column", [None, "id"])
def test_error_column_replaces_an_existing_column(spark, clock, id_column):
    df = spark.createDataFrame([(1, "bad", 0.5), (2, "good", 1.5)], "id long, text string, error double")
    out = make(FakeClient(responder=failing_on("bad")), clock).map(
        df, "text", "summary", error_column="error", id_column=id_column
    )
    assert out.columns.count("error") == 1
    assert out.schema["error"].dataType == StringType()
    assert by_id(out, "summary", "error") == {1: (None, "content filtered"), 2: ("<Summarize: good>", None)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_error_column_needs_its_own_name(spark, clock, id_column):
    df = simple_df(spark, [(1, "a")])
    with pytest.raises(ConfigError, match="different names"):
        make(FakeClient(), clock).map(df, "text", "summary", error_column="summary", id_column=id_column)


def test_without_error_column_failed_records_are_just_none(spark, clock):
    df = simple_df(spark, [(1, "bad"), (2, "good")])
    out = make(FakeClient(responder=failing_on("bad")), clock).map(df, "text", "summary")
    assert out.columns == ["id", "text", "summary"]
    assert [row["summary"] for row in out.collect()] == [None, "<Summarize: good>"]


@pytest.mark.parametrize("id_column", [None, "id"])
def test_on_error_raise_stops_a_spark_map_with_the_outputs(spark, clock, id_column):
    df = simple_df(spark, [(1, "good"), (2, "bad"), (3, "fine")])
    with pytest.raises(StepFailedError) as caught:
        make(FakeClient(responder=failing_on("bad")), clock, on_error="raise").map(
            df, "text", "summary", error_column="error", id_column=id_column
        )
    assert caught.value.outputs == ["<Summarize: good>", None, "<Summarize: fine>"]
    assert list(caught.value.failures) == [1]
    # ... and the partial DataFrame, so the replies that were paid for can be kept
    frame = caught.value.frame
    assert is_spark_dataframe(frame)
    assert frame.columns == ["id", "text", "summary", "error"]
    assert by_id(frame, "summary", "error") == {
        1: ("<Summarize: good>", None),
        2: (None, "content filtered"),
        3: ("<Summarize: fine>", None),
    }


# --- map with id_column: collect ids and texts, join the replies back ---------------------------------


@pytest.mark.parametrize(
    ("id_type", "ids"),
    [
        ("string", ["r-3", "r-1", "R-1", "r-10", ""]),
        ("long", [2**40 + 3, 7, -1, 0, 42]),
        ("int", [5, 4, 3, 2, 1]),
        ("date", [date(2024, 1, 3), date(2024, 1, 1), date(1999, 1, 1), date(2024, 1, 2), date(2000, 2, 29)]),
    ],
)
def test_map_with_id_column_joins_the_replies_back_by_id(spark, clock, id_type, ids):
    texts = ["alpha", None, "gamma", "  ", "epsilon"]
    scores = [1.5, 2.5, None, 4.5, 5.5]
    df = spark.createDataFrame(list(zip(ids, texts, scores, strict=True)), f"id {id_type}, text string, score double")
    client = FakeClient()
    out = make(client, clock).map(df, "text", "summary", id_column="id")

    assert out.columns == ["id", "text", "score", "summary"]
    assert out.schema.fields[:-1] == df.schema.fields
    assert out.schema["summary"] == STRING
    assert out.count() == 5
    assert by_id(out, "text", "score", "summary") == {
        i: (text, score, summarized(text)) for i, text, score in zip(ids, texts, scores, strict=True)
    }
    assert sorted(content_of(m) for m in client.calls["sync"]) == [
        "Summarize: alpha",
        "Summarize: epsilon",
        "Summarize: gamma",
    ]


def test_map_with_id_column_matches_replies_to_ids_in_a_shuffled_dataframe(spark, clock):
    rows = [(i * 7919 % 1000, f"text {i * 7919 % 1000}") for i in range(120)]
    df = spark.createDataFrame(rows, "id long, text string").repartition(3)
    out = make(FakeClient(), clock, map_batch_size=16).map(df, "text", "summary", id_column="id")
    assert by_id(out, "summary") == {i: (f"<Summarize: text {i}>",) for i, _ in rows}


def test_map_with_id_column_does_not_read_the_other_columns(spark, clock):
    """Only the id and text columns are collected: a column that fails whenever it's computed isn't touched."""
    df = simple_df(spark, [(1, "a"), (2, "b")]).withColumn(
        "boom", F.when(F.col("id") > -1, F.raise_error(F.lit("the whole row was collected")))
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert by_id(out.select("id", "summary"), "summary") == {1: ("<Summarize: a>",), 2: ("<Summarize: b>",)}
    assert out.columns == ["id", "text", "boom", "summary"]


def test_map_with_struct_id_column(spark, clock):
    df = spark.createDataFrame(
        [(Row(a=1, b="x"), "one"), (Row(a=1, b="y"), "two")], "id struct<a:int,b:string>, text string"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert by_id(out, "summary") == {Row(a=1, b="x"): ("<Summarize: one>",), Row(a=1, b="y"): ("<Summarize: two>",)}


def test_map_with_id_column_keeps_the_column_order(spark, clock):
    df = spark.createDataFrame([("a", 1, 0.5), ("b", 2, 1.5)], "text string, id long, score double")
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert out.columns == ["text", "id", "score", "summary"]
    assert out.schema.fields[:-1] == df.schema.fields


def test_repeated_ids_are_rejected_before_any_request(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b"), (1, "c")])
    client = FakeClient()
    with pytest.raises(ConfigError, match='id_column "id" has repeated values'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_repeated_string_ids_are_rejected(spark, clock):
    df = simple_df(spark, [("x", "a"), ("y", "b"), ("x", "c")], "id string, text string")
    with pytest.raises(ConfigError, match="repeated values"):
        make(FakeClient(), clock).map(df, "text", "summary", id_column="id")


def test_null_ids_are_rejected_before_any_request(spark, clock):
    df = simple_df(spark, [(1, "a"), (None, "b"), (3, "c")])
    client = FakeClient()
    with pytest.raises(ConfigError, match='id_column "id" has empty or NaN values; every row needs an id'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_nan_ids_are_rejected(spark, clock):
    df = spark.createDataFrame([(1.0, "a"), (float("nan"), "b"), (float("nan"), "c")], "id double, text string")
    client = FakeClient()
    with pytest.raises(ConfigError, match='id_column "id" has empty or NaN values'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_ids_that_are_not_simple_values_are_rejected(spark, clock):
    df = spark.createDataFrame([([1], "a"), ([2], "b")], "id array<int>, text string")
    client = FakeClient()
    with pytest.raises(ConfigError, match="simple values"):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_missing_id_column_is_rejected_before_any_request(spark, clock):
    df = simple_df(spark, [(1, "a")])
    client = FakeClient()
    with pytest.raises(ConfigError, match='Column "key" not found'):
        make(client, clock).map(df, "text", "summary", id_column="key")
    assert no_calls(client)


@pytest.mark.parametrize(("output_column", "error_column"), [("id", None), ("summary", "id")])
def test_output_and_error_columns_cannot_replace_the_id_column(spark, clock, output_column, error_column):
    df = simple_df(spark, [(1, "a")])
    with pytest.raises(ConfigError, match="can't replace the id_column"):
        make(FakeClient(), clock).map(df, "text", output_column, error_column=error_column, id_column="id")


@pytest.mark.parametrize(
    ("output_column", "error_column", "id_column"),
    [("id", None, "id"), ("summary", "id", "id"), ("summary", "summary", None), ("summary", "summary", "id")],
)
def test_clashing_column_names_are_rejected_before_any_request(spark, clock, output_column, error_column, id_column):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    client = FakeClient()
    with pytest.raises(ConfigError):
        make(client, clock).map(df, "text", output_column, error_column=error_column, id_column=id_column)
    assert no_calls(client)


def test_map_with_id_column_handles_column_names_with_dots(spark, clock):
    df = spark.createDataFrame([(1, "a"), (2, "b")], "id long, `review.text` string")
    out = make(FakeClient(), clock).map(df, "review.text", "summary", id_column="id")
    assert by_id(out, "summary") == {1: ("<Summarize: a>",), 2: ("<Summarize: b>",)}


# --- empty DataFrames ----------------------------------------------------------------------------------


@pytest.mark.parametrize("id_column", [None, "id"])
def test_map_on_an_empty_dataframe(spark, clock, id_column):
    df = spark.createDataFrame([], "id long, text string, score double")
    client = FakeClient()
    out = make(client, clock, strategies=STRATEGIES).map(
        df, "text", "summary", error_column="error", id_column=id_column
    )
    assert out.count() == 0
    assert out.columns == ["id", "text", "score", "summary", "error"]
    assert out.schema.fields[:3] == df.schema.fields
    assert out.schema.fields[3:] == [STRING, StructField("error", StringType(), True)]
    assert no_calls(client)


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_an_empty_dataframe_says_there_is_nothing_to_reduce(spark, clock, id_column):
    df = spark.createDataFrame([], "id long, text string")
    client = FakeClient()
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client, clock).run(df, "text", "summary", id_column=id_column)
    assert no_calls(client)


# --- reduce from a Spark column ------------------------------------------------------------------------


def test_reduce_a_spark_column_in_one_group(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b"), (3, "c")], "n int, summary string")
    client = FakeClient()
    result = make(client, clock).reduce(df, "summary")
    assert isinstance(result, ReduceResult)
    assert result.output == "<Combine: a\n\nb\n\nc>"
    assert result.levels == [["a", "b", "c"], [result.output]]
    assert [content_of(m) for m in client.calls["sync"]] == ["Combine: a\n\nb\n\nc"]


def test_reduce_a_spark_column_recursively_like_the_same_list(spark, clock):
    texts = ["t0", None, "t1", "  ", "t2", "t3", "", "t4"]
    df = spark.createDataFrame(list(enumerate(texts)), "n int, summary string")
    result = make(FakeClient(), clock, reduce_group_size=2).reduce(df, "summary")
    assert result.levels[0] == ["t0", "t1", "t2", "t3", "t4"]
    assert [len(level) for level in result.levels] == [5, 3, 2, 1]
    assert result.depth == 3
    same = make(FakeClient(), clock, reduce_group_size=2).reduce(["t0", "t1", "t2", "t3", "t4"])
    assert result == same


def test_reduce_a_non_string_spark_column(spark, clock):
    df = spark.createDataFrame([(1,), (None,), (3,)], "n int")
    result = make(FakeClient(), clock).reduce(df, "n")
    assert result.levels[0] == ["1", "3"]
    assert result.output == "<Combine: 1\n\n3>"


def test_reduce_a_spark_column_with_nothing_in_it(spark, clock):
    df = simple_df(spark, [(1, None), (2, " ")], "n int, summary string")
    client = FakeClient()
    with pytest.raises(MapReduceError, match="Nothing to reduce"):
        make(client, clock).reduce(df, "summary")
    assert no_calls(client)


def test_reduce_a_spark_dataframe_needs_a_column(spark, clock):
    df = simple_df(spark, [(1, "a")])
    client = FakeClient()
    with pytest.raises(ConfigError, match="Say which column"):
        make(client, clock).reduce(df)
    with pytest.raises(ConfigError, match='Column "summary" not found'):
        make(client, clock).reduce(df, "summary")
    assert no_calls(client)


def test_reduce_a_spark_column_with_a_dot_in_its_name(spark, clock):
    df = spark.createDataFrame([(1, "a"), (2, "b")], "id long, `map.output` string")
    assert make(FakeClient(), clock).reduce(df, "map.output").output == "<Combine: a\n\nb>"


def test_reduce_the_output_of_a_spark_map(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, None), (3, "c")])
    mr = make(FakeClient(), clock)
    mapped = mr.map(df, "text", "summary", id_column="id")
    result = mr.reduce(mapped.orderBy("id"), "summary")
    assert result.levels[0] == ["<Summarize: a>", "<Summarize: c>"]
    assert result.output == "<Combine: <Summarize: a>\n\n<Summarize: c>>"


# --- run: map then reduce ------------------------------------------------------------------------------


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_end_to_end_through_the_batch_api(spark, clock, id_column):
    df = spark.createDataFrame([(i, f"r{i}", i % 2 == 0) for i in range(12)], "id long, review string, even boolean")
    client = FakeClient()
    result = make(client, clock, strategies=STRATEGIES, map_batch_size=5, reduce_group_size=5).run(
        df, "review", "summary", id_column=id_column
    )

    assert isinstance(result, MapReduceResult)
    assert is_spark_dataframe(result.frame)
    assert result.frame.columns == ["id", "review", "even", "summary"]
    assert by_id(result.frame, "review", "even", "summary") == {
        i: (f"r{i}", i % 2 == 0, f"<Summarize: r{i}>") for i in range(12)
    }
    assert [len(level) for level in result.levels] == [12, 3, 1]
    assert result.depth == 2
    assert result.levels[0] == [f"<Summarize: r{i}>" for i in range(12)]  # the order the rows were read in
    assert result.output == result.levels[-1][0]
    assert result.output.startswith("<Combine: <Combine: <Summarize: r0>")
    assert result.map_failures == {}
    assert not client.calls["sync"] and not client.calls["async"]
    assert len(client.jobs) == 3 + 1 + 1  # map: jobs of 5, 5, 2; each reduce level fits in one job


def test_run_on_spark_with_a_collapse_prompt(spark, clock):
    df = simple_df(spark, [(i, f"r{i}") for i in range(5)])
    mr = MapReduce(
        FakeClient(),
        map_prompt="M {text}",
        collapse_prompt="Collapse {text}",
        reduce_prompt="Final {text}",
        reduce_group_size=2,
        strategies=("sync",),
        separator="|",
        show_progress=False,
    )
    result = mr.run(df, "text", "summary", id_column="id")
    assert result.levels[1] == ["<Collapse <M r0>|<M r1>>", "<Collapse <M r2>|<M r3>>", "<Collapse <M r4>>"]
    assert result.levels[2] == [
        f"<Collapse {result.levels[1][0]}|{result.levels[1][1]}>",
        f"<Collapse {result.levels[1][2]}>",
    ]
    assert result.output == f"<Final {result.levels[2][0]}|{result.levels[2][1]}>"


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_leaves_failed_and_empty_records_out_of_the_reduce(spark, clock, id_column):
    df = simple_df(spark, [(1, "good"), (2, "bad"), (3, None), (4, "fine")])
    client = FakeClient(responder=failing_on("bad"))
    result = make(client, clock).run(df, "text", "summary", error_column="error", id_column=id_column)

    assert list(result.map_failures) == [1]
    assert str(result.map_failures[1]) == "content filtered"
    assert result.levels[0] == ["<Summarize: good>", "<Summarize: fine>"]
    assert result.output == "<Combine: <Summarize: good>\n\n<Summarize: fine>>"
    assert by_id(result.frame, "summary", "error") == {
        1: ("<Summarize: good>", None),
        2: (None, "content filtered"),
        3: (None, None),
        4: ("<Summarize: fine>", None),
    }


def test_run_on_spark_keeps_the_types_of_the_other_columns(typed_df, clock):
    result = make(FakeClient(), clock).run(typed_df, "text", "summary")
    assert result.frame.schema.fields[:-1] == typed_df.schema.fields
    assert [tuple(row)[:-1] for row in result.frame.collect()] == [tuple(row) for row in typed_df.collect()]
    assert result.levels[0] == ["<Summarize: first>", "<Summarize: last>"]


@pytest.mark.parametrize("id_column", [None, "id"])
@pytest.mark.parametrize("step", ["map", "run"])
def test_missing_text_column_is_rejected_before_any_request(spark, clock, step, id_column):
    df = simple_df(spark, [(1, "a")])
    client = FakeClient()
    with pytest.raises(ConfigError, match='Column "review" not found. The DataFrame has: id, text'):
        getattr(make(client, clock), step)(df, "review", "summary", id_column=id_column)
    assert no_calls(client)


def test_column_names_are_case_sensitive_in_the_check(spark, clock):
    df = simple_df(spark, [(1, "a")])
    with pytest.raises(ConfigError, match='Column "Text" not found'):
        make(FakeClient(), clock).map(df, "Text", "summary")


def test_map_texts_rejects_a_spark_dataframe(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    client = FakeClient()
    with pytest.raises(TypeError, match=r"map_texts takes a list of texts; use map\(df, column, output_column\)"):
        make(client, clock).map_texts(df.select("text"))
    assert no_calls(client)


def test_spark_run_shows_map_and_per_level_reduce_progress(spark, clock, capsys):
    df = simple_df(spark, [(i, f"r{i}") for i in range(5)])
    make(FakeClient(), clock, show_progress=True, reduce_group_size=2).run(df, "text", "summary", id_column="id")
    err = capsys.readouterr().err
    assert "Map [sync]" in err
    assert "Reduce" in err
    for label in ("Level 1/3: 5 → 3", "Level 2/3: 3 → 2", "Level 3/3: 2 → 1"):
        assert label in err


# --- column_values and other pyspark objects -----------------------------------------------------------


@pytest.mark.parametrize("data", ["a text", ["a", "b"], ("a",)])
def test_column_values_rejects_a_column_for_anything_but_a_dataframe(data):
    with pytest.raises(ConfigError, match="column only applies when reducing a DataFrame"):
        column_values(data, "text")


def test_reduce_rejects_spark_objects_that_are_not_dataframes(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    client = FakeClient()
    mr = make(client, clock)
    with pytest.raises(TypeError, match="Pass a Spark DataFrame and a column name, not a Column"):
        mr.reduce(df.text)
    with pytest.raises(TypeError, match="not a GroupedData"):
        mr.reduce(df.groupBy("id"))
    with pytest.raises(ConfigError, match="column only applies"):
        mr.reduce(df.text, "text")
    assert no_calls(client)


def test_reduce_a_list_of_spark_rows_sends_each_row_as_json(spark, clock):
    rows = simple_df(spark, [(1, "a"), (2, "café")]).collect()
    result = make(FakeClient(), clock).reduce(rows)
    assert result.levels[0] == ['{"id": 1, "text": "a"}', '{"id": 2, "text": "café"}']


# --- without id_column the DataFrame goes through Arrow and back ---------------------------------------

TimestampNTZType = getattr(T, "TimestampNTZType", None)  # PySpark 3.4+

WIDE_FIELDS = [
    StructField("n", IntegerType(), False),
    StructField("big", LongType(), True),
    StructField("tiny", ByteType(), True),
    StructField("small", ShortType(), True),
    StructField("ratio", FloatType(), True),
    StructField("score", DoubleType(), True),
    StructField("price", DecimalType(38, 18), True),
    StructField("day", DateType(), True),
    StructField("ts", TimestampType(), True),
    *([StructField("local", TimestampNTZType(), True)] if TimestampNTZType else []),
    StructField("gap", DayTimeIntervalType(), True),
    StructField("raw", BinaryType(), True),
    StructField("flag", BooleanType(), True),
    StructField("tags", ArrayType(StringType(), True), True),
    StructField("attrs", MapType(StringType(), ArrayType(LongType(), True), True), True),
    StructField(
        "info",
        StructType(
            [
                StructField("when", TimestampType(), True),
                StructField("counts", MapType(StringType(), IntegerType(), True), True),
                StructField("items", ArrayType(StructType([StructField("k", StringType(), True)]), True), True),
            ]
        ),
        True,
    ),
    StructField("text", StringType(), True),
]
WIDE_SCHEMA = StructType(WIDE_FIELDS)


def wide_row(n, text, *, empty=False, **values):
    """One row of WIDE_SCHEMA: the given values, the defaults below for the rest (all None if ``empty``)."""
    defaults = {
        "big": 2**62 + n,
        "tiny": -128,
        "small": 32767,
        "ratio": 0.25,
        "score": -1e-300,
        "price": Decimal("-12345678901234567890.123456789012345678"),
        "day": date(1, 1, 1),
        "ts": datetime(1969, 12, 31, 23, 59, 59, 999999),
        "local": datetime(2024, 3, 10, 2, 30, 0, 1),  # doesn't exist in New York; fine without a time zone
        "gap": timedelta(days=-3, seconds=5, microseconds=7),
        "raw": bytearray(b"\x00\xff\x10"),
        "flag": False,
        "tags": ["x", None, "日本"],
        "attrs": {"a": [1, None, 2**40], "b": []},
        "info": Row(when=datetime(2000, 2, 29, 12), counts={"z": None}, items=[Row(k="v"), None]),
    }
    row = {"n": n, "text": text}
    for field in WIDE_FIELDS[1:-1]:
        row[field.name] = None if empty else values.get(field.name, defaults[field.name])
    return tuple(row[field.name] for field in WIDE_FIELDS)


WIDE_ROWS = [  # not in any sorted order
    wide_row(3, "third"),
    wide_row(1, None, empty=True),
    wide_row(4, "fourth", flag=True, tags=[], attrs={}, info=Row(when=None, counts=None, items=[])),
    wide_row(2, "  ", ts=datetime(2038, 1, 19, 3, 14, 8), day=date(9999, 12, 31), big=-(2**63)),
]


@pytest.fixture
def wide_df(spark):
    return spark.createDataFrame(WIDE_ROWS, WIDE_SCHEMA)


@pytest.fixture
def to_arrow_calls(spark, monkeypatch) -> list:
    """Records every DataFrame.toArrow call (and still collects)."""
    cls = type(spark.range(1))
    original = cls.toArrow
    calls = []

    def spy(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "toArrow", spy)
    return calls


@pytest.fixture
def no_arrow(spark, monkeypatch):
    """DataFrame.toArrow fails (an old pyarrow, a type Arrow can't carry...), so Rows are collected instead."""
    cls = type(spark.range(1))

    def unavailable(self, *args, **kwargs):
        raise RuntimeError("Arrow isn't available here")

    monkeypatch.setattr(cls, "toArrow", unavailable)


def test_map_rebuilds_through_arrow_keeping_every_type_value_and_the_row_order(wide_df, clock, to_arrow_calls):
    client = FakeClient()
    out = make(client, clock).map(wide_df, "text", "summary", error_column="error")

    assert len(to_arrow_calls) == 1  # the whole DataFrame was collected once, through Arrow
    assert out.schema.fields[:-2] == wide_df.schema.fields  # names, types (nested too) and nullability
    assert out.schema.fields[-2:] == [STRING, StructField("error", StringType(), True)]
    rows = out.collect()
    assert [tuple(row)[:-2] for row in rows] == [tuple(row) for row in wide_df.collect()]
    assert [row["n"] for row in rows] == [3, 1, 4, 2]
    assert [row["summary"] for row in rows] == ["<Summarize: third>", None, "<Summarize: fourth>", None]
    assert [row["error"] for row in rows] == [None] * 4
    assert [content_of(m) for m in client.calls["sync"]] == ["Summarize: third", "Summarize: fourth"]


def test_arrow_rebuild_keeps_extreme_values_exactly(spark, clock, to_arrow_calls):
    """Microseconds at both ends of the range, NaN, infinities, -0.0 and the widest decimals survive unchanged."""
    df = spark.sql(
        "SELECT * FROM VALUES "
        "(1, timestamp_micros(-1L), double('NaN'), 99999999999999999999.999999999999999999BD, 'a'), "
        "(2, timestamp_micros(253402300799999999L), double('-Infinity'), 0.000000000000000001BD, 'b'), "
        "(3, timestamp_micros(0L), -0.0D, CAST(NULL AS DECIMAL(38, 18)), 'c') "
        "AS t(id, ts, x, d, text)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary")
    assert len(to_arrow_calls) == 1
    got = out.select("id", F.unix_micros("ts"), "x", F.col("d").cast("string"), "summary").collect()
    assert [(row[0], row[1], row[3], row[4]) for row in got] == [
        (1, -1, "99999999999999999999.999999999999999999", "<Summarize: a>"),
        (2, 253402300799999999, "0.000000000000000001", "<Summarize: b>"),
        (3, 0, None, "<Summarize: c>"),
    ]
    assert math.isnan(got[0][2])
    assert got[1][2] == -math.inf
    assert got[2][2] == 0.0 and math.copysign(1.0, got[2][2]) == -1.0


def test_map_through_arrow_on_an_empty_dataframe_keeps_the_schema(spark, clock, to_arrow_calls):
    df = spark.createDataFrame([], WIDE_SCHEMA)
    client = FakeClient()
    out = make(client, clock, strategies=STRATEGIES).map(df, "text", "summary", error_column="error")
    assert len(to_arrow_calls) == 1
    assert out.count() == 0
    assert out.schema.fields == [*WIDE_FIELDS, STRING, StructField("error", StringType(), True)]
    assert no_calls(client)


def test_map_through_arrow_replaces_existing_columns_of_any_type(wide_df, clock, to_arrow_calls):
    out = make(FakeClient(), clock).map(wide_df, "text", "info", error_column="ATTRS")
    kept = [field for field in WIDE_FIELDS if field.name not in ("info", "attrs")]
    assert out.schema.fields == [*kept, StructField("info", StringType(), True), StructField("ATTRS", StringType())]
    assert [row["info"] for row in out.collect()] == ["<Summarize: third>", None, "<Summarize: fourth>", None]


def test_map_falls_back_to_rows_when_arrow_collection_fails(wide_df, clock, no_arrow, caplog):
    caplog.set_level(logging.DEBUG, logger="azure_mapreduce.frames")
    client = FakeClient()
    out = make(client, clock).map(wide_df, "text", "summary")
    assert "Collecting through Arrow failed (Arrow isn't available here); collecting rows instead." in caplog.text
    assert out.schema.fields[:-1] == wide_df.schema.fields
    assert [tuple(row)[:-1] for row in out.collect()] == [tuple(row) for row in wide_df.collect()]
    assert [row["summary"] for row in out.collect()] == ["<Summarize: third>", None, "<Summarize: fourth>", None]


def test_map_falls_back_to_rows_on_an_empty_dataframe(spark, clock, no_arrow):
    df = spark.createDataFrame([], "id long, text string")
    out = make(FakeClient(), clock).map(df, "text", "summary", error_column="error")
    assert out.count() == 0
    assert out.columns == ["id", "text", "summary", "error"]


def test_map_with_id_column_does_not_collect_the_whole_dataframe_through_arrow(spark, clock, to_arrow_calls):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert by_id(out, "summary") == {1: ("<Summarize: a>",), 2: ("<Summarize: b>",)}
    assert to_arrow_calls == []


# --- time zones: timestamps stay exact -----------------------------------------------------------------


def test_map_keeps_timestamps_in_the_repeated_daylight_saving_hour_inside_structs(spark, clock, new_york_time):
    df = spark.sql(
        "SELECT s AS id, named_struct('at', timestamp_seconds(s)) AS info, array(timestamp_seconds(s)) AS times, "
        "CAST(s AS STRING) AS text FROM VALUES (1730611800L), (1730615400L) AS t(s)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary")
    rows = out.select("id", F.unix_seconds("info.at"), F.unix_seconds(F.col("times")[0])).collect()
    assert [tuple(row) for row in rows] == [(1730611800,) * 3, (1730615400,) * 3]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: with a timestamp id_column the ids are collected as Rows (naive local datetimes), so the two "
    "instants of a repeated DST hour look like the same id and are rejected as repeated values",
)
def test_timestamp_ids_in_the_repeated_daylight_saving_hour_are_told_apart(spark, clock, new_york_time):
    df = spark.sql(
        "SELECT timestamp_seconds(s) AS id, CAST(s AS STRING) AS text FROM VALUES (1730611800L), (1730615400L) AS t(s)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert sorted(tuple(row) for row in out.select(F.unix_seconds("id"), "summary").collect()) == [
        (1730611800, "<Summarize: 1730611800>"),
        (1730615400, "<Summarize: 1730615400>"),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a timestamp id in the second occurrence of a repeated DST hour comes back from the Python side "
    "as the first occurrence, so the join misses it and its (paid-for) reply is silently dropped",
)
def test_a_timestamp_id_in_the_second_repeated_daylight_saving_hour_gets_its_reply(spark, clock, new_york_time):
    df = spark.sql(
        "SELECT timestamp_seconds(s) AS id, CAST(s AS STRING) AS text FROM VALUES (1730615400L), (1730619000L) AS t(s)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert sorted(tuple(row) for row in out.select(F.unix_seconds("id"), "summary").collect()) == [
        (1730615400, "<Summarize: 1730615400>"),  # 01:30 EST, after the clocks went back
        (1730619000, "<Summarize: 1730619000>"),
    ]


# --- nested cells in the text column are sent as JSON --------------------------------------------------

NESTED_SCHEMA = (
    "id long, tags array<string>, pair struct<a:int,b:string>, items array<struct<k:string,n:int>>, "
    "deep struct<inner:struct<xs:array<int>>>"
)
NESTED_ROWS = [
    (1, ["x", "café 日本"], Row(a=1, b="y"), [Row(k="v", n=None)], Row(inner=Row(xs=[1, 2]))),
    (2, [], Row(a=2, b=None), [], None),
    (3, None, None, None, Row(inner=None)),
]
NESTED_PROMPTS = {
    "tags": {1: '["x", "café 日本"]'},
    "pair": {1: '{"a": 1, "b": "y"}', 2: '{"a": 2, "b": null}'},
    "items": {1: '[{"k": "v", "n": null}]'},
    "deep": {1: '{"inner": {"xs": [1, 2]}}', 3: '{"inner": null}'},
}


@pytest.fixture(params=["arrow", "rows", "id_column"])
def collect_path(request):
    """How the text column is read: through Arrow, as Rows (Arrow unavailable), or with an id_column. Gives the
    id_column to pass."""
    if request.param == "rows":
        request.getfixturevalue("no_arrow")
    return "id" if request.param == "id_column" else None


@pytest.mark.parametrize("column", list(NESTED_PROMPTS))
def test_arrays_and_structs_in_the_text_column_are_sent_as_json(spark, clock, collect_path, column):
    df = spark.createDataFrame(NESTED_ROWS, NESTED_SCHEMA)
    client = FakeClient()
    out = make(client, clock).map(df, column, "summary", id_column=collect_path)
    expected = NESTED_PROMPTS[column]
    assert [content_of(m) for m in client.calls["sync"]] == [f"Summarize: {text}" for text in expected.values()]
    assert by_id(out, "summary") == {i: (summarized(expected.get(i)),) for i in (1, 2, 3)}
    assert out.schema[column] == df.schema[column]  # the input column itself is untouched


MAP_ARROW_BUG = pytest.mark.xfail(
    strict=True,
    reason="BUG: through Arrow a MapType cell comes back from to_pylist() as a list of (key, value) pairs, so it "
    'is sent as [["k", 1]] instead of the JSON object sent with an id_column (or when Rows are collected)',
)


@pytest.mark.parametrize(
    "collect_path", [pytest.param("arrow", marks=MAP_ARROW_BUG), "rows", "id_column"], indirect=True
)
def test_maps_in_the_text_column_are_sent_as_json_objects(spark, clock, collect_path):
    df = spark.sql(
        "SELECT 1L AS id, map('k', 1, 'j', 2) AS m, named_struct('attrs', map('a', array(1))) AS s, "
        "array(map('x', 'y')) AS am"
    )
    client = FakeClient()
    make(client, clock).map(df, "m", "summary", id_column=collect_path)
    make(client, clock).map(df, "s", "summary", id_column=collect_path)
    make(client, clock).map(df, "am", "summary", id_column=collect_path)
    prompts = [content_of(m).removeprefix("Summarize: ") for m in client.calls["sync"]]
    assert [json.loads(prompt) for prompt in prompts] == [{"k": 1, "j": 2}, {"attrs": {"a": [1]}}, [{"x": "y"}]]


def test_empty_arrays_and_maps_in_the_text_column_are_skipped(spark, clock, collect_path):
    df = spark.sql("SELECT 1L AS id, array() AS a, map() AS m UNION ALL SELECT 2L, NULL, NULL")
    client = FakeClient()
    for column in ("a", "m"):
        out = make(client, clock).map(df, column, "summary", id_column=collect_path)
        assert by_id(out, "summary") == {1: (None,), 2: (None,)}
    assert no_calls(client)


VARIANT_ARROW_BUG = pytest.mark.xfail(
    strict=True,
    reason="BUG: through Arrow a VARIANT cell comes back as {'value': bytes, 'metadata': bytes}, so the model is "
    "sent the variant's binary encoding instead of its JSON (sent correctly with an id_column or as Rows)",
)


@pytest.mark.parametrize(
    "collect_path", [pytest.param("arrow", marks=VARIANT_ARROW_BUG), "rows", "id_column"], indirect=True
)
def test_variant_cells_in_the_text_column_are_sent_as_their_json(spark, clock, collect_path):
    if not hasattr(T, "VariantType"):  # pragma: no cover - PySpark < 4
        pytest.skip("needs the VARIANT type")
    df = spark.sql("""SELECT 1L AS id, parse_json('{"a": 1, "b": [true, null]}') AS v""")
    client = FakeClient()
    out = make(client, clock).map(df, "v", "summary", id_column=collect_path)
    assert [content_of(m) for m in client.calls["sync"]] == ['Summarize: {"a":1,"b":[true,null]}']
    assert isinstance(out.schema["v"].dataType, T.VariantType)


@pytest.mark.xfail(
    strict=True,
    reason="BUG (inconsistency): a timestamp cell is sent as '2024-01-02 03:04:05+00:00' through Arrow (session "
    "time zone, with offset) but as '2024-01-02 03:04:05' with an id_column (driver-local, naive), so the prompt "
    "depends on whether id_column was given",
)
def test_a_timestamp_cell_is_sent_the_same_way_with_or_without_id_column(spark, clock):
    df = spark.sql(
        "SELECT 1L AS id, timestamp_seconds(1704164645L) AS ts, "
        "named_struct('at', timestamp_seconds(1704164645L)) AS info"
    )
    prompts = {}
    for id_column in (None, "id"):
        client = FakeClient()
        make(client, clock).map(df, "ts", "summary", id_column=id_column)
        make(client, clock).map(df, "info", "summary", id_column=id_column)
        prompts[id_column] = [content_of(m) for m in client.calls["sync"]]
    assert prompts[None] == prompts["id"]


def test_reduce_an_array_column_sends_each_cell_as_json(spark, clock):
    df = spark.createDataFrame(NESTED_ROWS, NESTED_SCHEMA)
    result = make(FakeClient(), clock).reduce(df, "tags")
    assert result.levels[0] == ['["x", "café 日本"]']
    assert make(FakeClient(), clock).reduce(df, "pair").levels[0] == ['{"a": 1, "b": "y"}', '{"a": 2, "b": null}']


# --- the id_column join keeps the column order; names are taken literally ------------------------------


@pytest.mark.parametrize("id_column", [None, "id"])
@pytest.mark.parametrize(
    ("schema", "output_column", "error_column", "columns"),
    [
        ("text string, id long, score double", "summary", None, ["text", "id", "score", "summary"]),
        ("score double, summary int, text string, id long", "summary", None, ["score", "text", "id", "summary"]),
        (
            "error int, text string, summary int, id long, z int",
            "summary",
            "error",
            ["text", "id", "z", "summary", "error"],
        ),
        ("id long, text string", "text", "error", ["id", "text", "error"]),
    ],
)
def test_output_columns_go_at_the_end_and_the_others_keep_their_order(
    spark, clock, id_column, schema, output_column, error_column, columns
):
    names = [part.split()[0] for part in schema.split(", ")]
    row = {"id": 1, "text": "a", "score": 0.5, "summary": 7, "error": 9, "z": 3}
    df = spark.createDataFrame([tuple(row[name] for name in names)], schema)
    out = make(FakeClient(), clock).map(df, "text", output_column, error_column=error_column, id_column=id_column)
    assert out.columns == columns
    kept = columns[: -2 if error_column else -1]
    assert [out.schema[name] for name in kept] == [df.schema[name] for name in kept]  # types unchanged
    assert out.collect()[0][output_column] == "<Summarize: a>"


def test_map_with_dotted_id_and_text_column_names(spark, clock):
    df = spark.createDataFrame(
        [(10, "a", 1), (20, None, 2), (30, "bad c", 3)], "`row.id` long, `review.text` string, `x.y` int"
    )
    out = make(FakeClient(responder=failing_on("bad")), clock).map(
        df, "review.text", "the.summary", error_column="the.error", id_column="row.id"
    )
    assert out.columns == ["row.id", "review.text", "x.y", "the.summary", "the.error"]
    assert {row["row.id"]: tuple(row)[1:] for row in out.collect()} == {
        10: ("a", 1, "<Summarize: a>", None),
        20: (None, 2, None, None),
        30: ("bad c", 3, None, "content filtered"),
    }


@pytest.mark.parametrize("id_column", [None, "row`id"])
def test_map_with_backticks_in_column_names(spark, clock, id_column):
    df = spark.createDataFrame([(1, "a", 5), (2, "b", 6)], "`row``id` long, `re``view` string, `sum``mary` int")
    assert df.columns == ["row`id", "re`view", "sum`mary"]
    out = make(FakeClient(), clock).map(df, "re`view", "sum`mary", error_column="err`or", id_column=id_column)
    assert out.columns == ["row`id", "re`view", "sum`mary", "err`or"]
    assert {row["row`id"]: tuple(row)[1:] for row in out.collect()} == {
        1: ("a", "<Summarize: a>", None),
        2: ("b", "<Summarize: b>", None),
    }


def test_reduce_a_spark_column_with_backticks_and_dots_in_its_name(spark, clock):
    df = spark.createDataFrame([(1, "a"), (2, "b")], "id long, `map.out``put` string")
    assert make(FakeClient(), clock).reduce(df, "map.out`put").output == "<Combine: a\n\nb>"


def test_run_with_dotted_names_through_the_batch_api(spark, clock):
    df = spark.createDataFrame([(i, f"r{i}") for i in range(4)], "`row.id` long, `review.text` string")
    result = make(FakeClient(), clock, strategies=STRATEGIES).run(df, "review.text", "the.summary", id_column="row.id")
    assert result.frame.columns == ["row.id", "review.text", "the.summary"]
    assert sorted(tuple(row) for row in result.frame.collect()) == [
        (i, f"r{i}", f"<Summarize: r{i}>") for i in range(4)
    ]
    assert result.levels[0] == [f"<Summarize: r{i}>" for i in range(4)]


def test_a_column_named_like_the_internal_join_key_is_left_alone(spark, clock):
    df = spark.createDataFrame([(1, "a", 10), (2, "b", 20)], "id long, text string, __azure_mapreduce_id__ int")
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert out.columns == ["id", "text", "__azure_mapreduce_id__", "summary"]
    assert by_id(out, "text", "__azure_mapreduce_id__", "summary") == {
        1: ("a", 10, "<Summarize: a>"),
        2: ("b", 20, "<Summarize: b>"),
    }


def test_an_id_column_named_like_the_internal_join_key(spark, clock):
    df = spark.createDataFrame([(1, "a"), (2, "b")], "__azure_mapreduce_id__ long, text string")
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="__azure_mapreduce_id__")
    assert out.columns == ["__azure_mapreduce_id__", "text", "summary"]
    assert sorted(tuple(row) for row in out.collect()) == [(1, "a", "<Summarize: a>"), (2, "b", "<Summarize: b>")]


def test_the_text_column_can_be_the_id_column(spark, clock):
    df = spark.createDataFrame([("a", 1), ("b", 2)], "text string, n int")
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="text")
    assert out.columns == ["text", "n", "summary"]
    assert sorted(tuple(row) for row in out.collect()) == [("a", 1, "<Summarize: a>"), ("b", 2, "<Summarize: b>")]


# --- output/error column names and spark.sql.caseSensitive ---------------------------------------------


@pytest.fixture
def case_sensitive_spark(spark):
    """Turn spark.sql.caseSensitive on for one test, and back to what it was afterwards."""
    key = "spark.sql.caseSensitive"
    previous = spark.conf.get(key)
    spark.conf.set(key, "true")
    try:
        yield spark
    finally:
        spark.conf.set(key, previous)


@pytest.mark.parametrize("id_column", [None, "id"])
def test_error_column_differing_only_in_case_replaces_the_column(spark, clock, id_column):
    df = spark.createDataFrame([(1, "bad", 0.5), (2, "good", 1.5)], "id long, text string, error double")
    out = make(FakeClient(responder=failing_on("bad")), clock).map(
        df, "text", "summary", error_column="ERROR", id_column=id_column
    )
    assert out.columns == ["id", "text", "summary", "ERROR"]
    assert out.schema["ERROR"].dataType == StringType()
    assert by_id(out, "summary", "ERROR") == {1: (None, "content filtered"), 2: ("<Summarize: good>", None)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_output_and_error_columns_differing_only_in_case_clash_before_any_request(spark, clock, id_column):
    df = simple_df(spark, [(1, "a")])
    client = FakeClient()
    with pytest.raises(ConfigError, match="The output and error columns need different names"):
        make(client, clock).map(df, "text", "summary", error_column="Summary", id_column=id_column)
    assert no_calls(client)


@pytest.mark.parametrize(("output_column", "error_column"), [("ID", None), ("summary", "Id")])
def test_output_columns_differing_only_in_case_from_the_id_column_are_rejected(
    spark, clock, output_column, error_column
):
    df = simple_df(spark, [(1, "a")])
    client = FakeClient()
    with pytest.raises(ConfigError, match="can't replace the id_column"):
        make(client, clock).map(df, "text", output_column, error_column=error_column, id_column="id")
    assert no_calls(client)


@pytest.mark.parametrize("id_column", [None, "id"])
def test_with_case_sensitive_spark_a_differently_cased_output_column_is_a_new_column(
    case_sensitive_spark, clock, id_column
):
    df = case_sensitive_spark.createDataFrame([(1, "a", 10), (2, "b", 20)], "id long, text string, summary int")
    out = make(FakeClient(), clock).map(df, "text", "Summary", error_column="SUMMARY", id_column=id_column)
    assert out.columns == ["id", "text", "summary", "Summary", "SUMMARY"]
    assert out.schema["summary"].dataType == IntegerType()
    assert by_id(out, "summary", "Summary", "SUMMARY") == {
        1: (10, "<Summarize: a>", None),
        2: (20, "<Summarize: b>", None),
    }


@pytest.mark.parametrize("id_column", [None, "id"])
def test_with_case_sensitive_spark_the_exactly_named_column_is_still_replaced(case_sensitive_spark, clock, id_column):
    df = case_sensitive_spark.createDataFrame([(1, "a", 10, 11)], "id long, text string, summary int, Summary int")
    out = make(FakeClient(), clock).map(df, "text", "Summary", id_column=id_column)
    assert out.columns == ["id", "text", "summary", "Summary"]
    assert tuple(out.collect()[0]) == (1, "a", 10, "<Summarize: a>")


def test_with_case_sensitive_spark_output_and_error_may_differ_only_in_case(case_sensitive_spark, clock):
    df = case_sensitive_spark.createDataFrame([(1, "a")], "id long, text string")
    out = make(FakeClient(), clock).map(df, "text", "ID", error_column="Id", id_column="id")
    assert out.columns == ["id", "text", "ID", "Id"]
    assert tuple(out.collect()[0]) == (1, "a", "<Summarize: a>", None)


def test_case_sensitive_fixture_restored_the_setting(spark):
    assert spark.conf.get("spark.sql.caseSensitive") == "false"


# --- repeated column names -----------------------------------------------------------------------------


def two_text_columns(spark):
    left = spark.createDataFrame([(1, "a")], "id long, text string")
    right = spark.createDataFrame([(1, "b")], "other long, text string")
    df = left.join(right, left.id == right.other)
    assert df.columns == ["id", "text", "other", "text"]
    return df


@pytest.mark.parametrize("id_column", [None, "id"])
@pytest.mark.parametrize("step", ["map", "run"])
def test_a_repeated_text_column_is_rejected_before_any_request(spark, clock, step, id_column):
    client = FakeClient()
    with pytest.raises(ConfigError, match='more than one column named "text"; rename or alias them first'):
        getattr(make(client, clock), step)(two_text_columns(spark), "text", "summary", id_column=id_column)
    assert no_calls(client)


def test_reducing_a_repeated_column_is_rejected(spark, clock):
    client = FakeClient()
    with pytest.raises(ConfigError, match='more than one column named "text"'):
        make(client, clock).reduce(two_text_columns(spark), "text")
    assert no_calls(client)


def test_a_repeated_id_column_is_rejected_before_any_request(spark, clock):
    df = spark.createDataFrame([(1, "a")], "id long, text string").crossJoin(spark.createDataFrame([(2,)], "id long"))
    client = FakeClient()
    with pytest.raises(ConfigError, match='more than one column named "id"'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


@pytest.mark.parametrize("id_column", [None, "id"])
def test_repeated_columns_named_like_the_output_are_all_replaced_and_others_kept(spark, clock, id_column):
    base = spark.createDataFrame([(1, "a")], "id long, text string")
    df = base.crossJoin(spark.createDataFrame([(5, 6)], "summary int, x int")).crossJoin(
        spark.createDataFrame([(7, 8)], "summary int, x int")
    )
    assert df.columns == ["id", "text", "summary", "x", "summary", "x"]
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column=id_column)
    assert out.columns == ["id", "text", "x", "x", "summary"]
    assert tuple(out.collect()[0]) == (1, "a", 6, 8, "<Summarize: a>")


# --- ids that can't be joined on -----------------------------------------------------------------------


def test_nan_float_ids_are_rejected_before_any_request(spark, clock):
    df = spark.createDataFrame([(1.0, "a"), (float("nan"), "b")], "id float, text string")
    client = FakeClient()
    with pytest.raises(ConfigError, match='id_column "id" has empty or NaN values; every row needs an id'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_nan_ids_are_rejected_in_a_dotted_id_column(spark, clock):
    df = spark.createDataFrame([(float("nan"), "a"), (2.0, "b")], "`row.id` double, text string")
    with pytest.raises(ConfigError, match='id_column "row.id" has empty or NaN values'):
        make(FakeClient(), clock).map(df, "text", "summary", id_column="row.id")


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a NaN inside a struct id passes the Python checks (nan != nan) but Spark's join matches NaN to "
    "NaN, so the rows are duplicated and get each other's replies",
)
def test_struct_ids_holding_nan_are_rejected_before_any_request(spark, clock):
    df = spark.createDataFrame(
        [(Row(a=1.0, b=float("nan")), "x"), (Row(a=1.0, b=float("nan")), "y")],
        "id struct<a:double,b:double>, text string",
    )
    client = FakeClient()
    with pytest.raises(ConfigError, match="NaN"):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


def test_struct_ids_with_null_fields_still_join(spark, clock):
    df = spark.createDataFrame(
        [(Row(a=1, b=None), "x"), (Row(a=2, b=None), "y")], "id struct<a:int,b:int>, text string"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert sorted((row["id"]["a"], row["summary"]) for row in out.collect()) == [
        (1, "<Summarize: x>"),
        (2, "<Summarize: y>"),
    ]


# --- a step that stops still hands back what was paid for ----------------------------------------------


def failing_reduce_on(marker: str, error: Exception | None = None):
    """Echoes the map prompts; fails every reduce prompt containing ``marker``."""

    def responder(messages):
        content = content_of(messages)
        if content.startswith("Combine") and marker in content:
            raise error or LLMRequestError("reduce group blocked", retryable=False, code="content_filter")
        return echo(messages)

    return responder


@pytest.mark.parametrize("id_column", [None, "id"])
def test_a_setup_error_during_a_spark_map_carries_the_partial_frame(spark, clock, id_column):
    def responder(messages):
        if "r2" in content_of(messages):
            raise LLMSetupError("The deployment was deleted.")
        return echo(messages)

    df = simple_df(spark, [(i, f"r{i}") for i in range(4)])
    with pytest.raises(LLMSetupError, match="deployment was deleted") as caught:
        make(FakeClient(responder=responder), clock).run(
            df, "text", "summary", error_column="error", id_column=id_column
        )
    assert caught.value.outputs == ["<Summarize: r0>", "<Summarize: r1>", None, None]
    frame = caught.value.frame
    assert is_spark_dataframe(frame)
    assert frame.columns == ["id", "text", "summary", "error"]
    assert by_id(frame, "summary", "error") == {
        0: ("<Summarize: r0>", None),
        1: ("<Summarize: r1>", None),
        2: (None, None),
        3: (None, None),
    }


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_attaches_the_mapped_frame_when_a_reduce_group_fails(spark, clock, id_column):
    df = simple_df(spark, [(1, "r1"), (2, "bad r2"), (3, "r3"), (4, "r4"), (5, "r5")])
    map_responder, reduce_responder = failing_on("bad"), failing_reduce_on("r1")

    def responder(messages):
        return (reduce_responder if content_of(messages).startswith("Combine") else map_responder)(messages)

    client = FakeClient(responder=responder)
    with pytest.raises(StepFailedError, match="1 of 2 groups failed in the reduce level 1") as caught:
        make(client, clock, reduce_group_size=2).run(df, "text", "summary", error_column="error", id_column=id_column)

    exc = caught.value
    mapped = ["<Summarize: r1>", "<Summarize: r3>", "<Summarize: r4>", "<Summarize: r5>"]
    assert exc.levels == [mapped]
    assert exc.outputs == [None, "<Combine: <Summarize: r4>\n\n<Summarize: r5>>"]
    assert list(exc.failures) == [0]
    assert list(exc.map_failures) == [1]
    assert str(exc.map_failures[1]) == "content filtered"
    assert is_spark_dataframe(exc.frame)
    assert exc.frame.columns == ["id", "text", "summary", "error"]
    assert by_id(exc.frame, "summary", "error") == {
        1: ("<Summarize: r1>", None),
        2: (None, "content filtered"),
        3: ("<Summarize: r3>", None),
        4: ("<Summarize: r4>", None),
        5: ("<Summarize: r5>", None),
    }


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_attaches_the_mapped_frame_when_the_reduce_hits_a_setup_error(spark, clock, id_column):
    df = simple_df(spark, [(i, f"r{i}") for i in range(3)])
    client = FakeClient(responder=failing_reduce_on("r0", LLMSetupError("The reduce deployment is gone.")))
    with pytest.raises(LLMSetupError, match="reduce deployment is gone") as caught:
        make(client, clock).run(df, "text", "summary", id_column=id_column)
    assert caught.value.map_failures == {}
    assert caught.value.outputs == [None]
    assert caught.value.levels == [[f"<Summarize: r{i}>" for i in range(3)]]
    assert by_id(caught.value.frame, "summary") == {i: (f"<Summarize: r{i}>",) for i in range(3)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_with_reduce_on_error_warn_records_the_dropped_group(spark, clock, id_column, caplog):
    df = simple_df(spark, [(i, f"r{i}") for i in range(5)])
    client = FakeClient(responder=failing_reduce_on("r0"))
    result = make(client, clock, reduce_group_size=2, reduce_on_error="warn").run(
        df, "text", "summary", id_column=id_column
    )
    assert not result.complete
    assert list(result.reduce_failures) == [1]
    assert list(result.reduce_failures[1]) == [0]
    assert str(result.reduce_failures[1][0]) == "reduce group blocked"
    assert result.map_failures == {}
    assert result.levels[1] == [
        "<Combine: <Summarize: r2>\n\n<Summarize: r3>>",
        "<Combine: <Summarize: r4>>",
    ]
    assert result.output == result.levels[-1][0]
    assert "The final text leaves out what those groups held" in caplog.text
    assert by_id(result.frame, "summary") == {i: (f"<Summarize: r{i}>",) for i in range(5)}


@pytest.mark.parametrize("id_column", [None, "id"])
def test_run_on_spark_where_every_reduce_group_fails_carries_the_frame(spark, clock, id_column):
    df = simple_df(spark, [(i, f"r{i}") for i in range(3)])
    client = FakeClient(responder=failing_reduce_on("Summarize"))
    with pytest.raises(MapReduceError, match="Every group failed at reduce level 1") as caught:
        make(client, clock, reduce_on_error="warn").run(df, "text", "summary", id_column=id_column)
    assert by_id(caught.value.frame, "summary") == {i: (f"<Summarize: r{i}>",) for i in range(3)}
    assert caught.value.map_failures == {}


def test_reduce_a_spark_column_with_reduce_on_error_warn_records_the_failure(spark, clock):
    df = spark.createDataFrame([(i, f"s{i}") for i in range(4)], "n int, summary string")
    client = FakeClient(responder=failing_reduce_on("s3"))
    result = make(client, clock, reduce_group_size=2, reduce_on_error="warn").reduce(df, "summary")
    assert not result.complete
    assert list(result.failures) == [1] and list(result.failures[1]) == [1]
    assert result.levels == [["s0", "s1", "s2", "s3"], ["<Combine: s0\n\ns1>"], [result.output]]


def test_reduce_a_spark_column_raises_by_default_when_a_group_fails(spark, clock):
    df = spark.createDataFrame([(i, f"s{i}") for i in range(4)], "n int, summary string")
    with pytest.raises(StepFailedError) as caught:
        make(FakeClient(responder=failing_reduce_on("s3")), clock, reduce_group_size=2).reduce(df, "summary")
    assert caught.value.outputs == ["<Combine: s0\n\ns1>", None]
    assert caught.value.levels == [["s0", "s1", "s2", "s3"]]
    assert not hasattr(caught.value, "frame")  # only run() has a mapped DataFrame to hand back
