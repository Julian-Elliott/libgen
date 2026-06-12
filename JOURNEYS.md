# 🧭 Customer Journeys

Every answer must hand the person their **next action** — a link, an
eligibility line, or a follow-up chip. If an answer names a service without a
route in, that's a bug (see the route-in audit, June 2026).

The design loop for each journey:

> **question → intent → live tool → answer → next action → follow-up chips**

## The journeys

| # | Who & what they ask | Intent | Tool | The answer must hand them |
|---|---|---|---|---|
| 1 | "Do you have Harry Potter audiobooks?" | Borrow a specific title | `search_catalogue` | Matches with format/year + detail links · **To borrow:** join-online / digital-membership links · full-results link |
| 2 | "Any good new crime novels?" | Discover something to read | `whats_new` | Newest titles · the same **To borrow** join link · see-more link |
| 3 | "Is Malvern library open now?" | Visit a branch today | `find_library` | 🟢/🔴 open-now + today's hours · address · facilities · **Libraries Unlocked hours + how to get access** · branch page |
| 4 | "A late-opening library with a café" | Pick the *right* branch | `graph_search` | Matching branches as **links with addresses** · "ask me 'is it open now?'" hint |
| 5 | "When does the van visit Abberley?" | Catch the mobile library | `mobile_library` | Stop times · "join on the van" · enquiry email · timetable link |
| 6 | "What's on this week?" | Take the kids somewhere | `library_events` | Events as links with dates/places · "most are free, just turn up" |
| 7 | "Can I read newspapers for free?" | Read/research from home | `online_hub` | Resource name **linked to its council page** · what you need · **app-store + web links** (PressReader, BorrowBox) · access steps |
| 8 | "How do I print from my phone?" | Print without a printer | `printing_help` | Steps with **join link** + **device-guide page link** · prices · the PaperCut kiosk caveat |
| 9 | "What do I need to sign up?" | Join (right tier, least effort) | `membership_help` | Each tier **linked to its own page** — digital membership links straight to instant sign-up |
| 10 | "Can I get in after work?" | Use the library out of hours | `libraries_unlocked` | Hours · the 11 branches · induction requirement · **join-online link for non-members** |
| 11 | Greeting / off-topic | Find out what this thing does | none (`HELP`) | Capability list + four starter chips |

## Journey-chaining (the chips)

Answers end with up to three **suggestion chips** (`NUDGES` in `app.py`) that
chain journeys together, EAST-style — e.g. a branch-hours answer (3) offers
"Tell me about Libraries Unlocked" (10); a catalogue answer (1) offers "Is it
on BorrowBox?" (7). The chip's answer must contain the links the nudge text
alludes to — the nudge itself stays short.

## Cross-journey guarantees

- **Provenance:** every answer footer shows when it was checked, which tool
  ran, and a source link.
- **Value receipt:** borrowing/online/printing answers show the £ saved
  (`value_receipt`).
- **Tone:** lead with what the person *can* do — never open with a bare "no"
  (`SYNTH_SYSTEM`). The print-from-home question is the canonical example:
  "send it from your phone, collect at any branch" beats "no, but…".
- **Degradation:** if the live source is down, say so plainly; with no
  `HF_TOKEN` the rendered data stands alone, so every link above lives in the
  *data/renderers* (`library_sources.py`, the `render_*` functions) — never
  only in LLM phrasing.

## Maintaining this

When adding or changing an answer, walk the loop: *what will the person do
next?* If the answer mentions a service, membership step, app or page — link
it, in the renderer or curated data, and check the chips chain somewhere
useful. Then re-run the battery in the PR #5 audit (a question per row above)
against `respond()`.
