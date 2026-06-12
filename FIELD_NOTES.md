# 📓 Field Notes — building a live library assistant on a small model

*Build Small Hackathon · what we built, and what we learned doing it.*

## The problem came from a real person

We didn't start with a model — we started with **Jack**, who manages library
resources in Worcester. His complaint, almost verbatim: *"the library doesn't shout
loudly enough about everything it offers."* That's not a marketing gap, it's an
**information gap** — and it turns out the UK government agrees. DCMS's 2024–25
research into library non-users found the single biggest fixable barrier is exactly
this: people **don't know the breadth** of what libraries do.

So the brief wrote itself: not "a chatbot for a library", but *a way to make the
library's own scattered, public information findable and inviting* — on a small model,
because the hackathon said ≤32B, and because honestly, you don't need a giant model
to do this well.

## The honest small-model bet

Our central design decision: **the intelligence lives in the retrieval, not the model.**
A 7–32B model is more than enough to *route* a question and *phrase* a grounded answer
— if you give it good, live, structured data. So we spent most of our effort on the
data and the graph, and let the model be small and swappable. The payoff: an honest fit
with the brief, and a **no-LLM fallback** that still answers from raw live data, so a
flaky endpoint never breaks the demo.

## What we learned mining the council's data

**1. The catalogue had a hidden clean API.** The Worcestershire catalogue runs on
SirsiDynix Enterprise — usually painful to scrape (JS-heavy). But probing it, we found
an undocumented **Atom feed** (`/client/rss/hitlist/wcc/qu=…`) that returns clean XML
with title, author, format, year and ISBN per result. One lucky find turned a scraping
nightmare into a five-line parser.

**2. "What you need to sign up" is the part everyone gets wrong.** Eligibility *varies
wildly* and is buried: digital membership is instant-by-postcode; full membership needs
a card; Print Your Way needs a PaperCut top-up; Libraries Unlocked needs an in-person
induction; PressReader needs a 30-day re-verify. We made eligibility a **first-class
field** on every answer. This is the bit users actually get stuck on.

**3. Crawlers lie unless you audit them.** Our first KB looked great until we audited it:
11 of 17 online resources had inherited **boilerplate bleed** — a related-links block
("Digital library membership: access free eBooks…") had been scraped as if it were each
resource's *own* access rule. So the bot would've told someone they could use Ancestry
from home (they can't — it's in-branch). We curated all 17 by hand. Lesson: **a confident
wrong answer is worse than no answer**, especially for a public service.

**4. Half the "services" were last summer's posters.** "World Book Day", "STEAMfest",
"Summer Reading Challenge" — dated campaign pages masquerading as standing services. We
added seasonal tagging so the assistant doesn't present a one-off as something you can do
today.

**5. Hours tables are not one shape.** The 11 Libraries-Unlocked branches use a
structured 3-column table (early-unlocked / core-staffed / late-unlocked); the community
libraries use plain "Monday: 9:30am to 5:00pm" text. We needed both parsers to get to
100% hours coverage and a working "open now?" check.

## Why a knowledge graph (GraphRAG), not just RAG

Flat retrieval answers "what are the opening hours of Malvern?" fine. It *can't* answer
*"a late-opening library with a café and meeting rooms"* — that's a join across three
facts. So we built a **GraphRAG-style** graph (inspired by microsoft/graphrag,
markitdown, IBM Docling): 320 nodes (branches, services, resources, facilities, areas,
memberships, 154 villages), 465 typed edges (HAS_FACILITY, OFFERS, REQUIRES, LOCATED_IN),
and community summaries. Because our source KB is already structured, we build the graph
**deterministically** — no per-node LLM calls, no API cost, fully reproducible. The
multi-hop query above traverses `Branch→OFFERS→Libraries Unlocked` and
`Branch→HAS_FACILITY→Facility` in one shot and lands on Malvern.

## Turning awareness into action (the EAST layer)

Knowing the DCMS barriers, we engineered against the Behavioural Insights Team's **EAST**
framework: **Easy** (quick-reply chips, exact sign-up steps), **Attractive** (a £-saved
"value receipt" — money-saving is DCMS's strongest reframe), **Social/Timely** (one
contextual "did you know?" nudge at the moment of contact). The nudges aren't decoration
— they're the mechanism that attacks the #1 barrier (awareness of breadth), and the
trace log tells Jack which ones actually convert.

## The Hive re-think: exclusion → provenance

Our first rule was "never touch thehiveworcester.org — it goes stale". Working
with Jack's team showed that rule threw away the only public description of
Worcester's biggest library asset: The Hive is Europe's first joint
university+public library, open 8:30am–10pm every day, home to the
Worcestershire Archive & Archaeology Service, 800+ study spaces, a Business &
IP Centre and a Youth Hub — and almost none of that lives on the council site.
**Staleness isn't a reason to exclude a source; it's a reason to tag it.** So
we crawl all 57 pages (`build_hive_kb.py`), record the *exact* offering of each
page, stamp every fact with its source URL + crawl date, and apply one
precedence rule: where the Hive and council sites conflict (hours, prices,
membership), the council wins and the answer says so. The site even flags its
own events page as static — so live events still come only from the council.

## Going copy-level: "do you have it?" → "here's how you get it"

A catalogue hit isn't an outcome; a borrowed book is. `where_to_get` extends
item search down to the **individual copies**: it reads the item's detail page
for per-branch holdings (which branch, shelf mark, on-shelf or out), and offers
the fastest route per format — walk in at the named branch, reserve free, or
borrow the eBook on BorrowBox *tonight* (the Atom feed's `Electronic Access`
field often carries the direct link). The detail page is the most fragile
markup we touch, so there are two independent parsers (holdings table, then a
branch-name-anchored text scan) and a hard honesty rule: **if neither parses,
say so and link the item page — never guess availability.**

## What surprised us

- A **single Atom endpoint** saved the hardest integration.
- The boring win — **"what you need to sign up"** — is probably the most *useful* feature.
- Building the graph from already-structured data made GraphRAG **cheap and reliable**,
  not the expensive thing people assume.
- Routing is harder than answering: "Harry Potter audiobooks" (catalogue) vs "audiobooks
  online" (BorrowBox) hinge on one word of context.

## Reproduce it

```bash
pip install -r requirements.txt gradio
python build_kb.py && python build_hive_kb.py && python graph_build.py
python app.py                                  # run (no-LLM mode without HF_TOKEN)
python library_sources.py                      # live self-test incl. copy-level §5
```

Everything reads official sources — `worcestershire.gov.uk` and the catalogue live,
plus every page of `thehiveworcester.org` with per-fact provenance and the council
taking precedence on conflicts. Public data, public good, on a model that fits on
a laptop.
