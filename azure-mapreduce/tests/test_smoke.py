import pandas as pd

from azure_mapreduce import MapReduce
from azure_mapreduce.errors import LLMRequestError

from .conftest import FakeClient


def make(client, clock, **options):
    return MapReduce(
        client,
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        show_progress=False,
        batch_poll_interval=1.0,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )


def test_batch_map_then_reduce(clock):
    client = FakeClient()
    df = pd.DataFrame({"review": [f"r{i}" for i in range(12)]})
    result = make(client, clock, map_batch_size=5, reduce_group_size=5).run(df, "review", "summary")
    assert result.frame["summary"].tolist() == [f"<Summarize: r{i}>" for i in range(12)]
    assert [len(level) for level in result.levels] == [12, 3, 1]
    assert not client.calls["sync"] and not client.calls["async"]
    assert len(client.jobs) == 3 + 1 + 1  # map: 3 jobs of 5, 5, 2; each reduce level fits in one job


def test_failed_batch_falls_back_to_async_then_sync(clock):
    def flaky_async(messages):
        if "r1" in messages[-1]["content"]:
            raise LLMRequestError("throttled", retryable=True)
        return "async"

    client = FakeClient(
        batch_statuses=("validating", "failed"), async_responder=flaky_async, sync_responder=lambda m: "sync"
    )
    out = make(client, clock, strategies=("batch", "async", "sync")).map_texts(["r0", "r1", "r2"])
    assert out == ["async", "sync", "async"]
