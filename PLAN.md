# 🗺️ PLAN — Worcestershire Libraries Live Assistant

The blueprint for the build: what it is, how it's wired, what's done, and what's next.
Companion docs: [README](README.md) · [SUBMISSION](SUBMISSION.md) · [IDEAS](IDEAS.md) · [FIELD_NOTES](FIELD_NOTES.md).

## Goal

Give Worcestershire Libraries one conversational voice that answers any resident
question from **live, official, source-cited data**, tells them **exactly what they
need to sign up**, and **surfaces the services they never knew existed** — on a ≤32B
model, locally-capable. The DCMS-evidenced answer to "the library doesn't shout
about what it offers."

## Architecture

```
                       ┌─────────────────────────────────────────┐
   user question  ──▶  │  route()   LLM JSON router + keyword       │
                       │            fallback (no-token safe)        │
                       └───────────────┬────────────────────────────┘
                                       ▼
        ┌──────────────── 10 tools ─────────────────┐   ┌── GraphRAG ──┐
        │ search_catalogue   (SirsiDynix Atom feed) │   │ local_search │
        │ whats_new          (newest titles)         │   │ global_search│
        │ find_library       (hours/open-now/facils) │◀──│ multi-hop    │
        │ mobile_library     (154 villages)          │   │ over graph   │
        │ library_events     (live events)           │   └──────────────┘
        │ online_hub         (PressReader/BorrowBox…) │   library_graph.json
        │ libraries_unlocked · printing · membership  │   320 nodes/465 edges
        └───────────────────┬────────────────────────┘
                            ▼
        synthesise (≤32B)  +  eligibility  +  £-receipt  +  EAST nudge
                            +  source link  +  open agent trace
                            ▼
                    answer  +  quick-reply chips
```

## Components & files

| Layer | File | Role |
|---|---|---|
| Ingestion | `build_kb.py` → `library_kb.json` | Crawl all 218 library URLs → 87 services / 23 branches / 17 resources, with eligibility & facilities |
| Graph | `graph_build.py` → `library_graph.json` | Deterministic GraphRAG: entities → relationships → communities → reports |
| Sources | `library_sources.py` | 10 live/KB tools + curated hub access + membership tiers |
| Retrieval | `graph_rag.py` | local/global graph search (multi-hop) |
| Traces | `trace.py` → `traces.jsonl` | per-turn structured agent trace (Open Trace badge) |
| App | `app.py` | router, renders, behaviour-change layer, Gradio UI |
| Custom UI | `server.py` + `index.html` | `gradio.Server` custom frontend (Off-Brand badge) |

## Build phases

- [x] **P1 — Live tools.** Catalogue (Atom), mobile, events, printing. Verified live.
- [x] **P2 — Comprehensive KB.** Full council-site crawl; eligibility + facilities + hours.
- [x] **P3 — New tools.** find_library (open-now), online_hub (+curated access), libraries_unlocked, membership_help, whats_new.
- [x] **P4 — GraphRAG.** 320-node graph; multi-hop "late library + café + meeting rooms" → Malvern.
- [x] **P5 — Behaviour-change layer.** EAST nudges, £-saved value receipt, quick-reply chips (DCMS/COM-B grounded).
- [x] **P6 — Traces.** JSONL logging + in-chat "how I answered" panel.
- [x] **P7 — KB refinement.** Curate all 17 hub resources, filter time-bound junk, tag seasonal pages.
- [ ] **P8 — Deploy** to the Space + `HF_TOKEN` + smoke-test (user action).
- [ ] **P9 — Stretch:** custom `gr.Server` frontend; a small fine-tune (🎯 badge); llama.cpp local run (🔌🦙).

## The behaviour-change layer (why, not just what)

Grounded in [DCMS *What works to engage non-users*](https://www.gov.uk/government/publications/what-works-to-engage-library-non-users) (COM-B) + the
Behavioural Insights Team **EAST** framework:

- **Easy** → quick-reply chips, exact "what you need", deep links.
- **Attractive** → £-saved "value receipt" (DCMS's strongest reframe: money-saving).
- **Social/Timely** → one contextual "did you know?" nudge at the moment of contact.
- **Awareness of breadth** (the #1 barrier) → the nudge engine surfaces hidden gems.
- **Measurement loop** → trace logs which nudges convert → evidence for Jack's campaigns.

## Trace & eval strategy (📡 Open Trace)

Every turn writes one JSON object to `traces.jsonl` (route → steps → answer → sources →
timing), close to the hackathon's own trace-dataset schema. `trace.push_to_hub(repo_id)`
uploads it as a dataset. The same trace renders in-chat as a "how I answered" panel
(🤖 Best Agent evidence). Future: a golden-question eval set scoring route accuracy +
source correctness.

## Badge roadmap

🤖 Best Agent ✅ · 📡 Open Trace ✅ · 📓 Field Notes ✅ · 🎨 Off-Brand ◑ (server.py) ·
🔌 Off-the-Grid / 🦙 Llama Champion ◑ (local llama.cpp) · 🎯 Well-Tuned ✗ (next).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| HF Inference flaky / no token | **No-LLM fallback** renders raw live data — demo never breaks |
| Council site HTML changes | Re-run `build_kb.py` (re-crawl) any time; tools fail soft |
| SirsiDynix slow/timeouts | per-call try/except; app degrades gracefully |
| Gradio 6 needs Py3.10+ | tested logic locally; boot-test on the Space first |
| Facility data sparse (e.g. "study space") | multi-hop honest about coverage; lead demo on café+meeting |

## Future work

Fine-tune a tiny model on Q→tool routing; live PressReader title search; FOI ingestion
(WhatDoTheyKnow) as a transparency feature; auto-generated "Shelf Life" podcast +
share-cards (see [IDEAS.md](IDEAS.md)); per-branch service mapping.
