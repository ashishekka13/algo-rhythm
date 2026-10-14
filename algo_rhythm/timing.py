"""Where a request's time goes: named stages, plus counted calls deep inside them (each rubberband run), for one
log line per request. Thread-local, so concurrent requests keep their own; outside a measured request it does nothing.

    with timing.request() as t:
        with t.stage("render"):
            ...                       # somewhere inside: with timing.count("rubberband"): ...
    log.info("... %s", t.summary())
"""
import contextlib
import json
import threading
import time

_local = threading.local()


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.stages, self.counts = {}, {}

    @contextlib.contextmanager
    def stage(self, name):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round(self.stages.get(name, 0.0) + time.perf_counter() - t0, 3)

    def summary(self, **extra):
        return json.dumps({"total_s": round(time.perf_counter() - self.t0, 3), "stages_s": self.stages,
                           "calls": {k: {"n": n, "s": round(s, 3)} for k, (n, s) in self.counts.items()}, **extra})


@contextlib.contextmanager
def request():
    """Measure the calls made in this thread until the block ends."""
    prev = getattr(_local, "timer", None)
    _local.timer = t = Timer()
    try:
        yield t
    finally:
        _local.timer = prev


@contextlib.contextmanager
def count(name):
    """Count one call to `name` and its time in the request being measured, if any."""
    t = getattr(_local, "timer", None)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        if t is not None:
            n, s = t.counts.get(name, (0, 0.0))
            t.counts[name] = (n + 1, s + time.perf_counter() - t0)
