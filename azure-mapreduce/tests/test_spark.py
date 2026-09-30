"""PySpark support: SparkFrame, column_values and MapReduce.map/run/reduce on Spark DataFrames.

One small local Spark session serves the whole module. Most tests use the loop strategy since the Spark adapter
doesn't care how the replies were made; the end-to-end runs go through the (fake) Batch API as well.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

pytest.importorskip("pyspark")

from pyspark.sql import Row, SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql.types import (  # noqa: E402
    ArrayType,
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    IntegerType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from azure_mapreduce import MapReduce, MapReduceResult, ReduceResult  # noqa: E402
from azure_mapreduce.errors import ConfigError, LLMRequestError, MapReduceError, StepFailedError  # noqa: E402
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: is_spark_dataframe says yes to a Spark Column (any attribute of a Column exists), so "
    "reduce(df.summary) asks for a column name instead of rejecting the Column",
)
def test_is_spark_dataframe_rejects_a_spark_column(spark):
    df = simple_df(spark, [(1, "a")])
    assert not is_spark_dataframe(df.text)


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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: rebuilding collected rows turns timestamps into naive local datetimes and back, so the two "
    "instants of a repeated DST hour collapse into one (untouched columns silently change)",
)
def test_map_keeps_timestamps_in_the_repeated_daylight_saving_hour(spark, clock, new_york_time):
    """2024-11-03 01:30 happens twice in New York (05:30 and 06:30 UTC); both instants must survive."""
    df = spark.sql(
        "SELECT timestamp_seconds(s) AS ts, CAST(s AS STRING) AS text FROM VALUES (1730611800L), (1730615400L) AS t(s)"
    )
    out = make(FakeClient(), clock).map(df, "text", "summary")
    assert [row[0] for row in out.select(F.col("ts").cast("long")).collect()] == [1730611800, 1730615400]


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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: output column names are compared case-sensitively, so 'Summary' is added next to 'summary' "
    "and Spark (case-insensitive by default) can no longer resolve either",
)
def test_output_column_differing_only_in_case_replaces_the_column(spark, clock):
    df = spark.createDataFrame([(1, "a", 10), (2, "b", 20)], "id long, text string, summary int")
    out = make(FakeClient(), clock).map(df, "text", "Summary")
    assert [name.lower() for name in out.columns].count("summary") == 1
    assert [row[0] for row in out.select("summary").collect()] == ["<Summarize: a>", "<Summarize: b>"]


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


def test_on_error_raise_stops_a_spark_map_with_the_outputs(spark, clock):
    df = simple_df(spark, [(1, "good"), (2, "bad"), (3, "fine")])
    with pytest.raises(StepFailedError) as caught:
        make(FakeClient(responder=failing_on("bad")), clock, on_error="raise").map(df, "text", "summary")
    assert caught.value.outputs == ["<Summarize: good>", None, "<Summarize: fine>"]
    assert list(caught.value.failures) == [1]


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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: the id_column join (on=id) moves the id column to the front instead of keeping the column order",
)
def test_map_with_id_column_keeps_the_column_order(spark, clock):
    df = spark.createDataFrame([("a", 1, 0.5), ("b", 2, 1.5)], "text string, id long, score double")
    out = make(FakeClient(), clock).map(df, "text", "summary", id_column="id")
    assert out.columns == ["text", "id", "score", "summary"]


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
    with pytest.raises(ConfigError, match='id_column "id" has empty values'):
        make(client, clock).map(df, "text", "summary", id_column="id")
    assert no_calls(client)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: NaN ids pass the Python uniqueness check (nan != nan) but Spark joins NaN to NaN, "
    "so rows are duplicated and get each other's replies",
)
def test_nan_ids_are_rejected(spark, clock):
    df = spark.createDataFrame([(1.0, "a"), (float("nan"), "b"), (float("nan"), "c")], "id double, text string")
    client = FakeClient()
    with pytest.raises(ConfigError):
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: clashing output/error/id column names are only checked in with_outputs, after every map "
    "request (possibly a 24h Batch job) has been paid for; the replies are then lost",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: with id_column the columns are read with F.col(name), which treats a dot as struct access, so "
    "a column that exists (and passed the check) can't be resolved",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: column_values selects the column by a parsed name, so a column with a dot in its name can't be "
    "reduced although it passes the column check",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="BUG: map_texts(spark_df) iterates the DataFrame's Columns and sends \"Column<'text'>\" to the model "
    "instead of rejecting a DataFrame",
)
def test_map_texts_rejects_a_spark_dataframe(spark, clock):
    df = simple_df(spark, [(1, "a"), (2, "b")])
    client = FakeClient()
    with pytest.raises((TypeError, ConfigError)):
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
