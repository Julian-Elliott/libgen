# 🗺️ PLAN — Worcestershire Libraries Live Assistant

The blueprint for the build: what it is, how it's wired, what's done, and what's next.
Companion docs: [README](README.md) · [SUBMISSION](SUBMISSION.md) · [IDEAS](IDEAS.md) · [FIELD_NOTES](FIELD_NOTES.md).

## Goal

Give Worcestershire Libraries one conversational voice that answers any resident
question from **official, source-cited data at every granularity** — down to
*which branch has the copy on its shelf right now* and *exactly what every page
of the Hive offers* — tells them **exactly what they need to sign up**, and
**surfaces the services they never knew existed** — on a ≤32B model,
locally-capable. The DCMS-evidenced answer to "the library doesn't shout about
what it offers." Built with/for Jack's library-resources team.

The granularity ladder:

| Level | Data | Tool |
|---|---|---|
| Service | 87 council service pages, eligibility-first | KB tools |
| Page | all 57 pages of thehiveworcester.org, exact offerings + provenance | `hive_info` |
| Item | live catalogue (Atom feed), incl. BorrowBox deep links | `search_catalogue` |
| Copy | per-branch holdings + status from the item's detail page | `where_to_get` |

## Architecture

```
                       ┌─────────────────────────────────────────┐
   user question  ──▶  │  route()   LLM JSON router + keyword       │
                       │            fallback (no-token safe)        │
                       └───────────────┬────────────────────────────┘
                                       ▼
        ┌──────────────── 12 tools ─────────────────┐   ┌── GraphRAG ──┐
        │ where_to_get       (copy-level: shelf @    │   │ local_search │
        │                     branch → reserve →     │   │ global_search│
        │                     BorrowBox tonight)     │   │ multi-hop    │
        │ hive_info          (57 Hive pages, exact   │◀──│ over graph   │
        │                     offer + provenance)    │   └──────────────┘
        │ search_catalogue   (SirsiDynix Atom feed)  │   library_graph.json
        │ whats_new          (newest titles)         │   349 nodes/502 edges
        │ find_library       (hours/open-now/facils) │   incl. 26 HiveService
        │ mobile_library     (154 villages)          │
        │ library_events     (live events)           │
        │ online_hub         (PressReader/BorrowBox…)│
        │ libraries_unlocked · printing · membership │
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
| Hive ingestion | `build_hive_kb.py` → `hive_kb.json` | Crawl all 57 Hive pages → exact per-page offerings, what-you-need, prices, levels; curated `hive_profile` of 26 extended capabilities, every fact source-tagged |
| Graph | `graph_build.py` → `library_graph.json` | Deterministic GraphRAG over BOTH KBs: entities → relationships → communities → reports (incl. 26 HiveService nodes; offline rebuild keeps villages) |
| Sources | `library_sources.py` | 12 live/KB tools + curated hub access + membership tiers + copy-level availability parser |
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
- [x] **P8 — The Hive, page-level.** Reverse the exclusion: crawl all 57 pages
  with provenance (`build_hive_kb.py`), `hive_info` tool, Hive in the graph
  (open-late, archives, 26 capabilities), council-wins-on-conflict rule.
- [x] **P9 — Copy-level "where to get it".** `where_to_get`: per-branch
  holdings/status from the item detail page (two parse strategies, fail-soft),
  free-reservation steps, BorrowBox deep links from the Atom feed's
  Electronic Access field. *Live-verify with `python library_sources.py`.*
- [ ] **P10 — Deploy** to the Space + `HF_TOKEN` + smoke-test (user action),
  incl. the new self-test §5 (copy-level) against the live catalogue.
- [ ] **P11 — Stretch:** custom `gr.Server` frontend; a small fine-tune (🎯 badge); llama.cpp local run (🔌🦙).

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
| Hive site goes stale again | every Hive fact shows its crawl date; council wins on conflict; `build_hive_kb.py` re-crawls in minutes |
| Detail-page holdings markup shifts | two independent parse strategies (table + branch-name text scan); if both miss, the answer says so and links the item page — never guesses |
| Gradio 6 needs Py3.10+ | tested logic locally; boot-test on the Space first |
| Facility data sparse (e.g. "study space") | multi-hop honest about coverage; lead demo on café+meeting |

## Future work

Fine-tune a tiny model on Q→tool routing; live PressReader title search; FOI ingestion
(WhatDoTheyKnow) as a transparency feature; auto-generated "Shelf Life" podcast +
share-cards (see [IDEAS.md](IDEAS.md)); per-branch service mapping.
