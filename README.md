---
title: Worcestershire Libraries Live Assistant
emoji: 📚
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 6.17.3
app_file: app.py
pinned: true
license: mit
short_description: Live, source-cited answers about your local library
tags:
  - track:backyard
  - track:wood
  - sponsor:openbmb
  - sponsor:openai
  - sponsor:nvidia
  - sponsor:modal
  - achievement:offgrid
  - achievement:welltuned
  - achievement:offbrand
  - achievement:llama
  - achievement:sharing
  - achievement:fieldnotes
---

# 📚 Worcestershire Libraries — Live Assistant

A small-model (**≤ 32B**) agent that answers real questions about Worcestershire
Libraries from official data **at every granularity** — service pages → every
page of the Hive's site → catalogue items → the individual copies on a branch's
shelf — mined live at question time wherever the source allows it.

> Built for the [Build Small Hackathon](https://huggingface.co/build-small-hackathon)
> · **Backyard AI** track. The "someone I know" is **Jack, a library resources
> manager in Worcester**, whose recurring complaint is that *the library never
> shouts loudly enough about everything it offers*. This is the megaphone.

## What it does

Ask in plain English and it routes your question to a live tool, reads what comes
back from the council's own systems, and explains it:

| You ask… | It mines… | Source |
|---|---|---|
| 📚 “How do I **get** Wolf Hall?” | the item's **copy-level holdings** — which branch has it on the shelf *now*, free reservation, or the eBook on BorrowBox tonight | `wcc.ent.sirsidynix.net.uk` |
| 📖 “Do you have Harry Potter audiobooks?” | the **SirsiDynix catalogue** (books, eBooks, audio, DVDs) | `wcc.ent.sirsidynix.net.uk` |
| 🐝 “What can I do at The Hive?” | **every page of the Hive's site** — archives & archaeology, 800+ study spaces, room hire, open 8:30am–10pm daily | `thehiveworcester.org` |
| 🚐 “When does the mobile library visit Abberley?” | the **mobile-library timetable** (154 villages) | `worcestershire.gov.uk` |
| 📅 “What's on this week?” | **events & activities** | `worcestershire.gov.uk` |
| 🖨️ “How do I print from my phone?” | **Print Your Way** steps & prices | `worcestershire.gov.uk` |

Every reply carries a **“checked live just now” footer** with the tool used and a
link back to the official page.

Each answer is designed around a customer journey — what the person is trying
to do and the next action the reply must hand them. See **[JOURNEYS.md](JOURNEYS.md)**.

Because this low‑cost tool can be built to run as a website, add-on or app, it
becomes an easy, always‑available point of contact. Each response can be tailored
to automatically highlight related services using analytics‑driven prompts, helping
surface both well‑used resources and lesser‑known parts of the library offer. The
result: a lightweight, affordable upgrade that improves access, boosts discovery,
and supports libraries without requiring major system changes.

## The honest small-model fit

The model never invents library facts. The intelligence lives in the **live
retrieval**; a 7B model is more than enough to *route* the question and *phrase*
the answer. That's a genuine fit with the brief — not a 32B model pretending to
know a catalogue it was never trained on.

- **Model:** `Qwen/Qwen2.5-7B-Instruct` by default (set `MODEL_ID` to swap up to
  any ≤32B model, e.g. `Qwen/Qwen2.5-32B-Instruct`).
- **Graceful degradation:** with no `HF_TOKEN`, the app still works in *no-LLM
  mode* — deterministic keyword routing + the raw live data. The demo never
  breaks.

## The Hive, with provenance

We originally excluded thehiveworcester.org because parts of it run stale. The
re-think: **include it page-by-page, but tag every fact with its source** —
because the Hive's site is the *only* place much of Worcester's extended offer
is described (Explore the Past archives & archaeology, Europe's first joint
university+public library, 800+ study spaces, room hire, the BIPC, the Youth
Hub). The rules that keep answers trustworthy:

- every Hive fact carries its **source page + crawl date** (`hive_kb.json`,
  rebuilt by `build_hive_kb.py`)
- where Hive and council pages conflict (hours, prices, membership), the
  **council page wins** — and the answer says which source it used
- the Hive's own events page is static, so **live events always come from the
  council site**

## Architecture

```
question ──▶ route()  (LLM JSON router, keyword fallback)
                 │
                 ▼
        library_sources.py  ──▶  the granularity ladder
                 │   service • 87 council service pages     (library_kb.json)
                 │   page    • 57 Hive pages, exact offer   (hive_kb.json)
                 │   item    • search_catalogue()           (live Atom feed)
                 │   copy    • where_to_get()               (live holdings:
                 │             shelf @ branch → reserve → BorrowBox tonight)
                 │   plus    • mobile_library() · library_events()
                 │           • find_library() · hive_info() · printing_help()
                 ▼
        synthesize() ──▶ warm, grounded answer + named-source footer
```

`library_sources.py` has **zero** Gradio/LLM dependencies and is independently
testable against the live sites: `python library_sources.py`.

## Run locally

```bash
pip install -r requirements.txt gradio
export HF_TOKEN=hf_xxx            # optional — omit for no-LLM / offgrid mode
export TRACE_DATASET=you/wpl-traces  # optional — persist usage analytics (see below)
python app.py                    # http://localhost:7860

# refresh the knowledge bases + graph any time:
python build_kb.py               # council pages   -> library_kb.json
python build_hive_kb.py          # every Hive page -> hive_kb.json
python graph_build.py            # both KBs        -> library_graph.json
```

### No-LLM / Offgrid mode (`achievement:offgrid`)

Run **without** `HF_TOKEN` and the assistant works entirely on its own — no
external AI API call is made. A deterministic keyword router (400+ patterns)
handles every question and returns the raw live data directly. This is the
`achievement:offgrid` mode: the library data still comes from the council's
own public website, but the intelligence layer requires no third-party model
service whatsoever.

```bash
python app.py          # no HF_TOKEN → keyword-only, zero AI API calls
```

The UI flags this mode clearly and all tools remain fully functional.

### Usage analytics (optional)

The Space's container is wiped on every reboot, so the local `traces.jsonl`
never accumulates. Set **`TRACE_DATASET`** (e.g. `you/wpl-traces`) with a
write-scoped `HF_TOKEN` and every turn is persisted to a **private** Hugging
Face Dataset — one small file per turn, written on a background worker so it
adds no latency and can never break an answer. Unset, it's a silent no-op.

That gives a queryable record of real behaviour to iterate on: questions that
fall through to the help text (`route.tool == "none"` — a coverage gap), turns
where the LLM router failed over to keyword matching (`route.router ==
"keyword"`), and flaky live sources (a step with `ok == false`). Questions are
user input, so the dataset is created private.

## Bonus quests in reach

- 🤖 **Best Agent** — a real route → live-tool → synthesise loop.
- 🎨 **Off-Brand** — custom-branded Worcestershire-teal UI.
- 📡 **Open Trace** — each answer exposes its routing + source, and with `TRACE_DATASET` set, every turn is persisted to a Hub dataset (see Usage analytics).
- 📓 **Field Notes** — write-up of building it with/for Jack.

## License

MIT — © Julian Elliott & Jack Hubbert.
