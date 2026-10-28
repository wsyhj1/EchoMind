"""Request-local timings and evidence snapshots; no external tracing service."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import re
from time import perf_counter_ns
from uuid import uuid4

from langchain_core.callbacks import AsyncCallbackHandler


_trace = ContextVar("echomind_trace", default=None)
_parent = ContextVar("echomind_span", default=None)


def safe_error(error):
    text = str(error)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "<REDACTED>", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer <REDACTED>", text, flags=re.I)
    return text[:1000]


def current_trace():
    return _trace.get()


def union_ms(spans):
    intervals = sorted((s["start_ms"], s["start_ms"] + s["duration_ms"]) for s in spans)
    end = total = 0.0
    for start, stop in intervals:
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return round(total, 3)


class RunTrace:
    def __init__(self, user_id, session_id, query, knowledge_base_id, config=None):
        self.run_id = str(uuid4())
        self.user_id = user_id
        self.session_id = session_id
        self.query = query
        self.knowledge_base_id = knowledge_base_id
        self.config = config or {}
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.start_ns = perf_counter_ns()
        self.spans = []
        self.contexts = []
        self.model_inputs = []
        self.ttft_ms = None
        self.answer = ""
        self.status = "running"
        self.error = None
        self.total_ms = None
        self.persisted = False
        self.personalization = None

    def elapsed(self):
        return (perf_counter_ns() - self.start_ns) / 1_000_000

    @contextmanager
    def activate(self):
        token = _trace.set(self)
        parent_token = _parent.set(None)
        try:
            yield self
        finally:
            _parent.reset(parent_token)
            _trace.reset(token)

    def begin(self, kind, source=None, **attributes):
        entry = {
            "span_id": str(uuid4()), "parent_span_id": _parent.get(),
            "kind": kind, "source": source, "start_ms": self.elapsed(),
            "duration_ms": None, "status": "running", "attributes": attributes,
        }
        self.spans.append(entry)
        return entry

    def end(self, entry, status="complete", error=None):
        if entry["duration_ms"] is None:
            entry["duration_ms"] = max(0.0, self.elapsed() - entry["start_ms"])
            entry["status"] = status
            if error:
                entry["error"] = safe_error(error)

    def add_context(self, stage, source_type, text, **metadata):
        self.contexts.append({"stage": stage, "source_type": source_type, "text": text, **metadata})

    def mark_answer(self):
        if self.ttft_ms is None:
            self.ttft_ms = self.elapsed()

    def finish(self, answer, status="complete", error=None):
        self.answer = answer
        self.status = status
        self.error = safe_error(error) if error else None
        for entry in self.spans:
            if entry["duration_ms"] is None:
                self.end(entry, "cancelled" if status == "cancelled" else "incomplete")
        self.total_ms = self.elapsed()

    def timings(self):
        closed = [s for s in self.spans if s["duration_ms"] is not None]
        stages = {}
        for kind in dict.fromkeys(s["kind"] for s in closed):
            items = [s for s in closed if s["kind"] == kind]
            stages[kind] = {"duration_ms": union_ms(items), "calls": len(items),
                            "work_ms": round(sum(s["duration_ms"] for s in items), 3),
                            "failures": sum(s["status"] not in ("complete", "skipped") for s in items)}
        generations = [s for s in closed if s["kind"] == "model" and s["attributes"].get("answer_call")]
        if generations:
            last = max(generations, key=lambda s: s["start_ms"])
            stages["generation"] = {"duration_ms": round(last["duration_ms"], 3), "calls": 1,
                                    "failures": int(last["status"] != "complete")}
        by_source = {}
        for source in dict.fromkeys(s["source"] for s in closed):
            by_source[source or "unknown"] = {
                kind: {"duration_ms": union_ms([s for s in closed if s["source"] == source and s["kind"] == kind]),
                       "calls": sum(s["source"] == source and s["kind"] == kind for s in closed)}
                for kind in dict.fromkeys(s["kind"] for s in closed if s["source"] == source)
            }
        return {"server_total_ms": round(self.total_ms if self.total_ms is not None else self.elapsed(), 3),
                "ttft_ms": round(self.ttft_ms, 3) if self.ttft_ms is not None else None,
                "stages": stages, "by_source": by_source}

    def snapshot(self):
        return {"version": 1, "run_id": self.run_id, "user_id": self.user_id,
                "session_id": self.session_id, "query": self.query,
                "knowledge_base_id": self.knowledge_base_id, "config": self.config,
                "started_at": self.started_at, "status": self.status, "error": self.error,
                "answer": self.answer, "timings": self.timings(), "spans": self.spans,
                "contexts": self.contexts, "model_inputs": self.model_inputs,
                "personalization": self.personalization}


@contextmanager
def measure(kind, source=None, **attributes):
    trace = current_trace()
    if trace is None:
        yield {"attributes": attributes, "status": "complete"}
        return
    entry = trace.begin(kind, source, **attributes)
    token = _parent.set(entry["span_id"])
    try:
        yield entry
    except BaseException as exc:
        trace.end(entry, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", exc)
        raise
    else:
        trace.end(entry, entry["status"] if entry["status"] != "running" else "complete")
    finally:
        _parent.reset(token)


def timed(kind, source=None):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with measure(kind, source) as entry:
                result = await function(*args, **kwargs)
                if kind == "embedding" and not result:
                    entry["status"] = "failed"
                return result
        return wrapped
    return decorate


def record_context(stage, source_type, text, **metadata):
    trace = current_trace()
    if trace is not None:
        trace.add_context(stage, source_type, text, **metadata)


class ModelTimingCallback(AsyncCallbackHandler):
    """A new callback instance per request; never share active spans across users."""
    raise_error = True
    def __init__(self, trace):
        self.trace = trace
        self.active = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        limit = self.trace.config.get('chat_limits',{}).get('model_calls')
        if limit is not None and len(self.trace.model_inputs)>=limit:
            raise RuntimeError('本轮模型调用预算已达上限')
        entry = self.trace.begin("model", "llm", model=(metadata or {}).get("ls_model_name"),
                                 node=(metadata or {}).get("langgraph_node"))
        self.active[str(run_id)] = entry
        snapshot = [{"role": getattr(m, "type", "unknown"), "content": m.content}
                    for m in messages[0]] if messages else []
        self.trace.model_inputs.append({"span_id": entry["span_id"], "messages": snapshot})

    async def on_llm_end(self, response, *, run_id, **kwargs):
        entry = self.active.pop(str(run_id), None)
        if entry is None:
            return
        try:
            message = response.generations[0][0].message
            entry["attributes"]["answer_call"] = bool(message.content) and not bool(message.tool_calls)
            entry["attributes"]["usage"] = getattr(message, "usage_metadata", None)
        except (AttributeError, IndexError):
            pass
        self.trace.end(entry)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        entry = self.active.pop(str(run_id), None)
        if entry is not None:
            self.trace.end(entry, "failed", error)
