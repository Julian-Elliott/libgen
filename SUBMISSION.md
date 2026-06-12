# 📚 Worcestershire Libraries — Live Assistant · Submission Pack

> **Build Small Hackathon · Track: 🏡 Backyard AI** (with a 🍄 Thousand Token Wood crossover)
> One‑line: *The library that "doesn't shout about what it offers" — given a voice that knows everything, built from public data, running on a small model.*

---

## 1. The fable (open the pitch with this)

> Jack is a library resources manager in Worcester. His recurring frustration:
> **"the library doesn't shout loudly enough about everything it offers."**
>
> Everything a resident needs to know *is* public — but it's scattered across 200+
> council pages, a 1990s‑style catalogue, PDF van timetables and buried FOI logs.
> So we gave the library one calm, conversational voice that mines all of it **live**,
> tells you exactly **what you need to sign up**, and — the DCMS‑evidenced part —
> nudges you toward the services you never knew existed. On a ≤32B model. On a laptop.

That's the whole story: **civic data democratisation, small and local.**

## 2. What it is (elevator)

A Gradio app where you ask in plain English and a small‑model agent answers from
**live official data + a local knowledge graph**:

- 📚 Find a book / eBook / audiobook (live **SirsiDynix catalogue**)
- 📍 Branch **"open now?"**, hours, toilets, parking, café (live council pages)
- 🚐 **Mobile library** times for any of **154 villages**
- 📅 **What's on** this week
- 💻 **Free online** — newspapers (PressReader), eBooks (BorrowBox), family history (Ancestry) — with *exactly how to access each*
- 🖨️ **Print Your Way** from your phone
- 🧭 **Multi‑hop graph** questions: *"a late‑opening library with a café and meeting rooms"* → Malvern

Every answer carries **what you need to sign up**, a **£‑saved value receipt**, one
**"did you know?" nudge**, a live **source link**, and an open **agent trace**.

## 3. Why we win (competitive read)

The field is mostly *either* emotional *or* technical *or* genuinely‑used — rarely all
three, and **none mine live, verifiable real‑world data**. We hit every judging criterion:

| Judging criterion | Our evidence |
|---|---|
| Problem is specific & real | Jack's own words; a real council with real underused services |
| The person actually used it | Named beneficiary + real patrons; demo on his branch |
| Honest small‑model fit | The graph does the reasoning; the 7–32B model just routes + phrases |
| Polish | Branded UI, streaming, chips, source‑cited every time |
| (depth nobody else has) | Real **GraphRAG** + live multi‑source mining, not one prompt |

Borrowed insight from the field: shareable output wins Community Choice (Lolaby/whimsy
entries) → our **value‑receipt / hot‑take share‑card** is our shareable hook; strong
Backyard entries **lead with a face + a quote** → the demo opens on Jack.

## 4. Bonus badges we're claiming

| Badge | Status | Evidence |
|---|---|---|
| 🤖 **Best Agent** | ✅ strong | 10 tools, LLM router + keyword fallback, graph search, visible traces |
| 📡 **Open Trace** | ✅ | every turn → `traces.jsonl` (shareable schema); one‑click push to Hub |
| 📓 **Field Notes** | ✅ | `IDEAS.md`, this pack, + a short build blog |
| 🎨 **Off‑Brand** | ◑ partial | custom Worcestershire‑teal UI (stretch: `gr.Server`) |
| 🔌 **Off the Grid** / 🦙 **Llama Champion** | ◑ optional | runs fully local via llama.cpp (documented); Space default uses HF Inference |
| 🎯 **Well‑Tuned** | ✗ future | a small fine‑tune is the next badge to grab |
| Specials | 🎬 Best Demo · 🃏 Judges' Wildcard (data democratisation) · 🗳️ Community Choice |

## 5. Demo video script (~75s)

| t | Shot | Voiceover |
|---|---|---|
| 0–8s | Jack on camera (or his quote on screen) | "Jack runs library resources in Worcester. He says the library never shouts about what it offers." |
| 8–18s | Type *"Is Malvern library open now?"* → 🔴/🟢 + facilities | "So we built it a voice. It checks the council site live — open now, toilets, parking." |
| 18–30s | *"Can I read newspapers for free?"* → PressReader steps + titles | "It knows what's free online — and exactly how to sign up." |
| 30–45s | *"A late‑opening library with a café and meeting rooms"* → Malvern | "Ask by *features* — that's a knowledge‑graph traversal a chatbot can't do." |
| 45–58s | Show the £‑saved receipt + a "did you know?" nudge | "Every answer shows what you saved, and surfaces a service you didn't know existed — straight from the DCMS playbook on re‑engaging non‑users." |
| 58–70s | Expand the agent trace | "And it shows its working — open traces, all from public data, on a small model." |
| 70–75s | Logo + Space URL | "Your library. Out loud." |

## 6. Social post (pick one)

**X / Bluesky:**
> Libraries are full of free stuff nobody knows about. So we gave Worcestershire's a
> voice: ask it anything, it mines the council site + catalogue **live**, tells you
> exactly how to sign up, and shows what you just saved 💷 — all on a ≤32B model.
> #BuildSmall 🏡📚 [link]

**LinkedIn:**
> For the Build Small Hackathon we built a "Backyard AI" for a friend who manages
> library resources in Worcester. His problem: the library doesn't shout about
> everything it offers. Our answer: a small‑model agent over a live knowledge graph
> of the *whole* service — books, mobile van, events, free newspapers, "what you need
> to sign up" — grounded in DCMS behaviour‑change research. Public data, public good,
> running on a laptop. [link]

## 7. Tech summary (for the write‑up / Q&A)

```
question → route (LLM JSON + keyword fallback)
        → 12 live/KB tools  ── SirsiDynix catalogue (Atom) down to COPY level
        →                   ── all 57 Hive pages, exact offer (provenance‑tagged)
        → GraphRAG search   ── council pages (87 services / 23 branches / 17 resources)
        → synthesise (≤32B)    library_graph.json: 349 nodes · 502 edges · 26 Hive services
        → answer + eligibility + £‑receipt + EAST nudge + source + open trace
```

- **Inspiration:** microsoft/graphrag, microsoft/markitdown, IBM Docling.
- **Evidence spine:** DCMS/Ipsos *What works to engage non‑users* (COM‑B) + BIT **EAST**.
- **Honest constraint fit:** small model + big graph; **no‑LLM fallback** so the demo never breaks.
- **Sources:** worcestershire.gov.uk + the live catalogue (checked at question time), plus every page of thehiveworcester.org crawled with per‑fact provenance — council wins on conflicts.

## 8. Go‑live checklist (for Jack, Friday AM)

- [ ] Create Space `build-small-hackathon/wpl-discovery` (Gradio, public).
- [ ] Push: `app.py`, `library_sources.py`, `graph_rag.py`, `trace.py`,
      `library_kb.json`, `hive_kb.json`, `library_graph.json`, `requirements.txt`, `README.md`, `LICENSE`.
      *(build_kb.py / graph_build.py are build‑time only — optional to include.)*
- [ ] Add Space secret **`HF_TOKEN`** (read scope) → enables the model. *Without it the
      app still runs in no‑LLM mode, so a missing token won't block Jack's test.*
- [ ] Optional: set `MODEL_ID=Qwen/Qwen2.5-32B-Instruct` to run at the cap.
- [ ] Smoke‑test the 6 example chips; confirm a live catalogue hit + a graph multi‑hop.
- [ ] Send Jack the link + 3 starter questions; capture his reaction for the demo video.

**Refresh data any time:** `python build_kb.py && python graph_build.py` (re‑crawls the
council site and rebuilds the graph — keeps everything current).
