"""LangChain-style map-reduce over pandas and PySpark DataFrames with Azure OpenAI.

Requests go to the Azure Batch API first, then to async chat completions if that fails, then to a plain
loop of chat completions.
"""

from .client import AzureChatClient, BatchJob, foundry_base_url
from .errors import ConfigError, LLMRequestError, LLMSetupError, MapReduceError, StepFailedError
from .mapreduce import MapReduce, MapReduceResult, ReduceResult, reduce_levels
from .runners import STRATEGIES

__version__ = "0.1.0"

__all__ = [
    "STRATEGIES",
    "AzureChatClient",
    "BatchJob",
    "ConfigError",
    "LLMRequestError",
    "LLMSetupError",
    "MapReduce",
    "MapReduceError",
    "MapReduceResult",
    "ReduceResult",
    "StepFailedError",
    "foundry_base_url",
    "reduce_levels",
]
