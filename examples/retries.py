"""Run with `floe run examples/retries.py`; no network calls or retry sleeps."""

from floe import RetryPolicy

WORKFLOW = "retry-demo"
SCHEDULE = ["hourly"]
_calls = 0


def fetch():
    global _calls
    _calls += 1
    print(f"fetch attempt {_calls}")
    if _calls == 1:
        raise TimeoutError("simulated transient failure")
    print("fetch succeeded")


def publish():
    print("publish runs only after fetch succeeds")


tasks = {"fetch": fetch, "publish": publish}
dag = {"fetch": set(), "publish": {"fetch"}}
retries = {"fetch": RetryPolicy(max_attempts=3, delay_seconds=0, retry_on=(TimeoutError,))}
