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
short_description: Live answers about your library — books, mobile van, events, printing.
---

# 📚 Worcestershire Libraries — Live Assistant

A small-model (**≤ 32B**) agent that answers real questions about Worcestershire
Libraries by **mining live data at question time** from official sources only.

> Built for the [Build Small Hackathon](https://huggingface.co/build-small-hackathon)
> · **Backyard AI** track. The "someone I know" is **Jack, a library resources
> manager in Worcester**, whose recurring complaint is that *the library never
> shouts loudly enough about everything it offers*. This is the megaphone.

## What it does

Ask in plain English and it routes your question to a live tool, reads what comes
back from the council's own systems, and explains it:

| You ask… | It mines… | Source |
|---|---|---|
| 📖 “Do you have Harry Potter audiobooks?” | the **SirsiDynix catalogue** (books, eBooks, audio, DVDs) | `wcc.ent.sirsidynix.net.uk` |
| 🚐 “When does the mobile library visit Abberley?” | the **mobile-library timetable** (154 villages) | `worcestershire.gov.uk` |
| 📅 “What's on this week?” | **events & activities** | `worcestershire.gov.uk` |
| 🖨️ “How do I print from my phone?” | **Print Your Way** steps & prices | `worcestershire.gov.uk` |

Every reply carries a **“checked live just now” footer** with the tool used and a
link back to the official page.

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

## Why not "the Hive"?

We deliberately **avoid scraping thehiveworcester.org** — that content is
unreliable and often years out of date. Only the council website and the live
catalogue are used, so answers are trustworthy and current.

## Architecture

```
question ──▶ route()  (LLM JSON router, keyword fallback)
                 │
                 ▼
        library_sources.py  ──▶  live HTTP to council + catalogue
                 │                 • search_catalogue()  (Atom feed)
                 │                 • mobile_library()     (village pages)
                 │                 • library_events()     (events page)
                 │                 • printing_help()      (printing page)
                 ▼
        synthesize() ──▶ warm, grounded answer + live source footer
```

`library_sources.py` has **zero** Gradio/LLM dependencies and is independently
testable against the live sites: `python library_sources.py`.

## Run locally

```bash
pip install -r requirements.txt gradio
export HF_TOKEN=hf_xxx            # optional — omit for no-LLM mode
python app.py                    # http://localhost:7860
```

## Bonus quests in reach

- 🤖 **Best Agent** — a real route → live-tool → synthesise loop.
- 🎨 **Off-Brand** — custom-branded Worcestershire-teal UI.
- 📡 **Open Trace** — each answer exposes its routing + source (easy to publish).
- 📓 **Field Notes** — write-up of building it with/for Jack.

## License

MIT — © Julian Elliott & Jack Hubbert.
