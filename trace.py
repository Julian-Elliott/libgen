"""
trace.py — capture a structured agent trace per turn.

Two payoffs:
  • 📡 Open Trace badge — every answer's reasoning is logged to traces.jsonl in a
    shareable schema and can be pushed to the Hub.
  • 🤖 Best Agent — the trace is also shown in-chat as a "How I answered" panel,
    so the agent's route → tool → retrieve → synthesise loop is visible.

The schema is intentionally close to the hackathon's own trace datasets:
one JSON object per user turn, with ordered steps.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

TRACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces.jsonl")


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
        return self

    def to_markdown(self) -> str:
        r = self.d["route"]
        steps = " → ".join(s["type"] for s in self.d["steps"]) or "—"
        src = self.d["sources"][0] if self.d["sources"] else ""
        srcline = f" · [source]({src})" if src else ""
        return (
            "\n\n<details><summary>🔎 How I answered (agent trace)</summary>\n\n"
            f"- **Route:** `{r.get('tool','?')}` via {r.get('router','?')} "
            f"({r.get('latency_ms',0)} ms)\n"
            f"- **Steps:** {steps}\n"
            f"- **Model:** {self.d['model']} · **Total:** {self.d['total_ms']} ms · "
            f"{self.d['ts']}{srcline}\n\n"
            "<sub>Logged openly to `traces.jsonl` for the 📡 Open Trace badge.</sub>\n"
            "</details>"
        )

    def to_dict(self) -> dict:
        return self.d


def push_to_hub(repo_id: str, token: str | None = None, path: str = TRACE_PATH):
    """Upload traces.jsonl as a dataset file (optional, needs a token)."""
    from huggingface_hub import HfApi
    HfApi(token=token).upload_file(
        path_or_fileobj=path, path_in_repo="traces.jsonl",
        repo_id=repo_id, repo_type="dataset")
