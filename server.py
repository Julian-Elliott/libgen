"""
server.py — a fully custom frontend via `gradio.Server`  (🎨 Off-Brand badge).

Reuses the SAME backend as app.py (one source of truth: app.respond), but serves
a hand-built HTML/CSS/JS chat UI instead of the default Gradio look. The frontend
talks to this server with the Gradio JS client and streams the answer.

NOTE: app.py (Gradio Blocks) remains the tested, go-live default. This is the
custom-UI variant — **smoke-test it on the Space first** (we couldn't boot Gradio
locally on Python 3.9). To use it on a Space, set `app_file: server.py` in README.

Run:  python server.py    →  http://localhost:7860
"""

from __future__ import annotations

import os

from fastapi.responses import HTMLResponse
from gradio import Server

import app as backend  # reuse respond(), tools, model wrapper, traces

HERE = os.path.dirname(os.path.abspath(__file__))
server = Server()


@server.api(name="ask")
def ask(message: str):
    """Stream the assistant's growing markdown answer to the custom frontend."""
    last = ""
    for text, _chips in backend.respond(message, []):
        last = text
        yield text
    if not last:
        yield "Sorry — I didn't catch that. Try asking about a book, a branch, " \
              "events, or printing."


@server.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    server.launch(server_name="0.0.0.0", server_port=7860)
