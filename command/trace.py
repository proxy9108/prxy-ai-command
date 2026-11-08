"""Trace: 'why did this appear?' as a query. The runner stamps each agent run with a run id and puts it in a context variable; every data-source call
and every LLM call made while that run is active is recorded against it. A finding stores the run that raised it, so
finding -> run -> queries -> model calls is a join, not an archaeology project. Query text is scrubbed and kept for the retention window (24 h)."""
import contextvars, time
from .redact import scrub

RUN = contextvars.ContextVar("prxy_run", default=None)       # {'store': Store, 'id': int, 'n': int}
MAX_PER_RUN = 80


def begin(store, run_id):
    return RUN.set({"store": store, "id": run_id, "n": 0})


def current():
    r = RUN.get()
    return r["id"] if r else None


def record(kind, ref, detail="", ms=0):
    r = RUN.get()
    if not r:
        return
    r["n"] += 1
    if r["n"] > MAX_PER_RUN:
        return
    try:
        r["store"].trace_add(r["id"], kind, scrub(str(ref))[:400], scrub(str(detail))[:300], int(ms))
    except Exception:
        pass


class timed:
    """with timed('prom', expr) as t: ...  records one row with the elapsed time and 'ok' or the error."""
    def __init__(self, kind, ref):
        self.kind, self.ref, self.t0 = kind, ref, time.time()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        record(self.kind, self.ref, "ok" if et is None else f"{et.__name__}: {str(ev)[:150]}", (time.time() - self.t0) * 1000)
        return False
