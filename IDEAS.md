# IDEAS - bringing "your library, out loud" to life

A living backlog of ways to get the public *interested* in what their library
offers, not just *informed*. Everything here is gradeable against the evidence
([EAST](https://oecd-opsi.org/toolkits/east-four-simple-ways-to-apply-behavioural-insights/):
Easy · Attractive · Social · Timely; [DCMS COM-B non-user research](https://www.gov.uk/government/publications/what-works-to-engage-library-non-users/what-works-to-engage-library-non-users))
and stays inside the hackathon rules (**≤32B models, local where possible**).

The unifying move: the same source-of-truth (`library_kb.json` + `library_graph.json`)
that powers the assistant **also auto-generates the media** below. One graph, many voices.

Legend - **Impact** (behaviour-change lever) · **Effort** (hack-weekend feasibility) · **Badge** it helps win.

---

## 1. AI-generated media (the core idea)

The library can't afford a media team. A ≤32B model + small local TTS/diffusion *is* the media team.

| Idea | What it is | Impact (EAST) | Effort | Badge |
|---|---|---|---|---|
| **"Shelf Life" AI podcast** | NotebookLM-style **two-host audio** auto-built from the KB - e.g. a 3-min episode "5 things Malvern Library does that you didn't know". Script by the 32B model, voices by local TTS (Piper/Kokoro). | Attractive + intellectual register | ◐◐ | Best Demo, Off-the-Grid |
| **"Did You Know?" Shorts** | 15–30s vertical video, one hidden gem, hook in the first second ("You're paying £9.99 for newspapers your library gives you free"). Auto-scripted per service; captions + a stock/branch image. | Attractive + Social, Gen-Z register | ◐◐ | Best Demo |
| **Personalised micro-clip** | User picks a segment ("job-seeker / parent / saver / curious") → tool generates a tailored 30s script/share-card for *their* situation. | Tailored messaging (DCMS) | ◐ | Best Agent |
| **"Library Minute" radio drop** | A 60s audio spot for local radio / the council podcast, regenerated weekly from new events + new books. | Timely + Social | ◐ | Best Demo |
| **The £-saved "value receipt" card** | A shareable image: *"This chat saved you £28.98 - 1 hardback + a month of magazines."* Operationalises DCMS's money-saving reframe. | Attractive (the #1 reframe) | ◐ | Best Demo, Community Choice |

## 2. Match the register to the audience (DCMS segmentation)

The user's instinct - *sometimes a Short, sometimes something for an intellectual* - is exactly the
DCMS finding that messaging must be **tailored per segment**. Map content style → segment:

| Segment (why they don't come) | Register | Format |
|---|---|---|
| Digitally-confident sceptics ("libraries are dated") | Sharp, stat-led, slightly provocative | Short + the value-receipt |
| Parents / families | Warm, practical, time-saving | "What's on this week near you" reel |
| Family historians / retirees | Long-form, rich | "Shelf Life" deep-dive podcast (Ancestry, local archive) |
| Job-seekers / new starters | Reassuring, step-by-step | Tailored micro-clip + how-to |
| The simply curious | Playful, surprising | "Did You Know?" Short, the oracle (§4) |

## 3. Facilitate it *inside the tool* (so the app is the studio)

- **"Make me a clip" button** on any answer → generates a script + share-card (and, with TTS, an audio file) right there. Turns every Q&A into shareable content. *(Satisfies the hackathon's social-post requirement automatically.)*
- **Weekly auto-episode**: a scheduled job assembles new events + `whats_new` hot-takes into a "Shelf Life" episode + a Short, posted to [@worcslibraries](https://www.facebook.com/Worcslibraries/).
- **QR-to-clip**: a poster/shelf QR opens the tool pre-asked ("What can this library do for me?") and offers the clip - the *Timely* nudge at the point of being in the building.
- **Conversion logging** (via the trace layer) tells Jack which clip/topic actually drives sign-ups → an evidence loop, not vanity metrics.

## 4. Whimsy & delight (Thousand Token Wood crossover)

The same engine can wander somewhere weirder - a second, joyful entry point:

- **The Library Oracle** - describe your week, get a book "prescribed" with a one-line hot take + a reservation link.
- **Blind Date with a Book** - the model writes a teasing, spoiler-free dating-profile for a real catalogue title; swipe to reserve.
- **"The Library of You"** - answer 3 questions, get a tiny generated "membership of an imaginary branch curated for you" (real services mapped to a whimsical persona).
- **Mobile-van adventure map** - the 154-village graph rendered as a hand-drawn trail (ties to the hackathon's own "Thousand Token Wood" aesthetic).

## 5. Small-model production stack (keeps it hackathon-legal + earns badges)

| Job | ≤32B / local option |
|---|---|
| Scripts, hot-takes, podcast dialogue | the app's main ≤32B model (Qwen2.5-32B etc.), local via **llama.cpp** |
| Voices (TTS) | Kokoro-82M / Piper - tiny, local, fast (Tiny Titan ≤4B) |
| Images / thumbnails | FLUX.1-schnell / SDXL-Turbo (small, fast) |
| Video assembly | ffmpeg + captions (deterministic, no model) |

Doing media generation on **small local models** flips the whole pitch: *"a county library with no
budget produces NLB-grade outreach on a laptop"* - and stacks Off-the-Grid + Llama Champion + Tiny Titan.

## 6. Distribution channels

In-app share-card · [@worcslibraries](https://x.com/worcslibraries) FB/X/IG/YouTube · kiosk loop in-branch ·
shelf QR codes · the council e-newsletter · partner schools & job centres.

---

## Shortlist to actually demo this weekend

1. **Value receipt (£ saved) share-card** - highest impact-per-effort, directly evidence-based, instant social-post.
2. **"Did You Know?" Short generator** - one button, one hidden gem, vertical clip. The wow moment.
3. **"Shelf Life" 3-min podcast for one branch** - proves the long-form/intellectual register and the auto-from-graph pipeline.

Everything else is backlog. Add freely - this file is the bank, not the plan.
