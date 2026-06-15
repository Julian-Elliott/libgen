"""
trace.py - capture a structured agent trace per turn.

Three payoffs:
  • Open Trace badge - every answer's reasoning is logged to traces.jsonl in a
    shareable schema and can be pushed to the Hub.
  • Best Agent - the trace is also shown in-chat as a "How I answered" panel,
    so the agent's route → tool → retrieve → synthesise loop is visible.
  • Behaviour analytics - when TRACE_DATASET is set, every turn is persisted to
    a Hugging Face Dataset so the data survives the Space's reboots. That's the
    raw material for iterating on real usage: which questions fall through to the
    help text (`route.tool == "none"`), when the LLM router fails over to keyword
    matching (`route.router == "keyword"`), and which live sources are flaky
    (a step with `ok == False`).

The schema is intentionally close to the hackathon's own trace datasets:
one JSON object per user turn, with ordered steps.

Persistence is **opt-in and best-effort**: set `TRACE_DATASET` (e.g.
"your-user/wpl-traces") and have a write-scoped `HF_TOKEN`; otherwise it's a
no-op and only the local traces.jsonl is written. Uploads run on a single
background worker (so they never add latency to an answer and never conflict on
the dataset's git history) and any failure is swallowed - analytics must never
break the assistant. The dataset is created **private** (questions are user
input and may contain personal detail).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone

TRACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces.jsonl")

# Opt-in persistence config. TRACE_DATASET unset -> local-file-only (no-op).
TRACE_DATASET = os.environ.get("TRACE_DATASET")
_HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")


class Trace:
    def __init__(self, question: str, model: str):
        self.d = {
            "trace_id": uuid.uuid4().hex[:12],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app": "worcs-libraries-live-assistant",
            "question": question,
            "model": model,
            "route": {},
            "steps": [],
            "answer": "",
            "sources": [],
            "total_ms": 0,
        }
        self._t0 = time.time()

    def set_route(self, tool: str, args: dict, router: str, ms: int):
        self.d["route"] = {"tool": tool, "args": args, "router": router,
                           "latency_ms": ms}

    def step(self, kind: str, **kw):
        self.d["steps"].append({"type": kind, **kw})
        return self

    def finish(self, answer: str, sources: list):
        self.d["answer"] = answer
        self.d["sources"] = [s for s in sources if s]
        self.d["total_ms"] = int((time.time() - self._t0) * 1000)
        return self

    def save(self, path: str = TRACE_PATH):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.d, ensure_ascii=False) + "\n")
        except Exception:
            pass
        enqueue_trace(self.d)
        return self

    def to_markdown(self) -> str:
        r = self.d["route"]
        steps = " → ".join(s["type"] for s in self.d["steps"]) or "-"
        src = self.d["sources"][0] if self.d["sources"] else ""
        srcline = f" · [source]({src})" if src else ""
        return (
            "\n\n<details open><summary>How I answered (agent trace)</summary>\n\n"
            f"- **Route:** `{r.get('tool','?')}` via {r.get('router','?')} "
            f"({r.get('latency_ms',0)} ms)\n"
            f"- **Steps:** {steps}\n"
            f"- **Model:** {self.d['model']} · **Total:** {self.d['total_ms']} ms · "
            f"{self.d['ts']}{srcline}\n\n"
            "<sub>Logged openly to `traces.jsonl` for the Open Trace badge.</sub>\n"
            "</details>"
        )

    def to_dict(self) -> dict:
        return self.d


def push_to_hub(repo_id: str, token: str | None = None, path: str = TRACE_PATH):
    """Upload the whole local traces.jsonl as one dataset file (legacy/manual).

    For continuous persistence that survives reboots, prefer the automatic
    per-turn upload below (set TRACE_DATASET). This whole-file push overwrites
    the dataset's traces.jsonl and only contains the current container's turns.
    """
    from huggingface_hub import HfApi
    HfApi(token=token).upload_file(
        path_or_fileobj=path, path_in_repo="traces.jsonl",
        repo_id=repo_id, repo_type="dataset")


# --------------------------------------------------------------------------- #
# Continuous persistence - one small file per turn, on a single background
# worker. Per-turn files (never an append) sidestep read-modify-write races and
# can't lose earlier turns when the Space is rebooted, since each is its own
# object in the dataset repo.
# --------------------------------------------------------------------------- #

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
_worker_started = False
_worker_lock = threading.Lock()


def _worker():
    import io
    from huggingface_hub import HfApi

    api = HfApi(token=_HF_TOKEN)
    try: # idempotent; private because questions are user input
        api.create_repo(repo_id=TRACE_DATASET, repo_type="dataset",
                        private=True, exist_ok=True)
    except Exception:
        pass
    while True:
        record = _queue.get()
        try:
            blob = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            stamp = re.sub(r"[^0-9T]", "", record.get("ts", "")) or "0"
            name = f"traces/{stamp}-{record.get('trace_id', 'x')}.jsonl"
            api.upload_file(
                path_or_fileobj=io.BytesIO(blob), path_in_repo=name,
                repo_id=TRACE_DATASET, repo_type="dataset",
                commit_message="add trace")
        except Exception:
            pass # best-effort: analytics must never break the assistant
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="trace-uploader",
                             daemon=True).start()
            _worker_started = True


def enqueue_trace(record: dict):
    """Queue a trace for upload. No-op unless TRACE_DATASET + HF_TOKEN are set."""
    if not (TRACE_DATASET and _HF_TOKEN):
        return
    _ensure_worker()
    try:
        _queue.put_nowait(dict(record)) # copy: caller may keep mutating
    except queue.Full:
        pass # shed load rather than block or grow unboundedly
