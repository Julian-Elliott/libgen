"""
Worcestershire Libraries — Live Assistant  (Build Small Hackathon)

A small-model (<=32B) civic agent that answers real questions about
Worcestershire Libraries from LIVE official data + a local knowledge graph.

  • Live tools  — catalogue, mobile library, events, branch hours/facilities
  • Knowledge graph (GraphRAG-style) — multi-hop "which late library has a café?"
  • Curated detail — exactly what you need to sign up for each service
  • Behaviour-change layer — EAST nudges + £-saved "value receipt" (DCMS-evidenced)
  • Open agent traces — every answer logs its reasoning to traces.jsonl

Run:  HF_TOKEN=...  python app.py      (works with no token in "no-LLM" mode too)
Model: set MODEL_ID (default Qwen/Qwen2.5-7B-Instruct, <=32B).
"""

from __future__ import annotations

import json
import os
import re
import time

import gradio as gr

import library_sources as ls
import graph_rag
from trace import Trace

# --------------------------------------------------------------------------- #
# Model (<= 32 billion params)
# --------------------------------------------------------------------------- #

MODEL_ID = os.environ.get("MODEL_ID", "Qwen/Qwen2.5-7B-Instruct")
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")

_client = None
def get_client():
    global _client
    if _client is None:
        from huggingface_hub import InferenceClient
        _client = InferenceClient(model=MODEL_ID, token=HF_TOKEN, timeout=60)
    return _client


def llm(messages, *, max_tokens=512, temperature=0.3, stream=False):
    return get_client().chat_completion(
        messages, max_tokens=max_tokens, temperature=temperature, stream=stream)


# --------------------------------------------------------------------------- #
# Tools the agent can call
# --------------------------------------------------------------------------- #

TOOLS = {
    "search_catalogue": {
        "desc": "Find a specific book, eBook, audiobook or DVD the library holds. "
                "args: {\"query\": \"<title / author / subject>\"}",
        "fn": lambda a: ls.search_catalogue(a.get("query", ""), limit=6)},
    "whats_new": {
        "desc": "Newest titles in a genre, for fun recommendations. "
                "args: {\"genre\": \"<genre/topic>\"}",
        "fn": lambda a: ls.whats_new(a.get("genre") or a.get("query"))},
    "find_library": {
        "desc": "A branch's opening hours ('open now?'), address and facilities "
                "(toilets, parking, café, study space). args: {\"name\": \"<branch>\"}",
        "fn": lambda a: ls.find_library(a.get("name") or a.get("query"))},
    "mobile_library": {
        "desc": "When/where the mobile library van visits a village. "
                "args: {\"place\": \"<village>\"}",
        "fn": lambda a: ls.mobile_library(a.get("place") or a.get("query", ""))},
    "library_events": {
        "desc": "Upcoming events, activities, clubs and sessions. "
                "args: {\"query\": \"<optional keyword/place>\"}",
        "fn": lambda a: ls.library_events(a.get("query") or None, limit=8)},
    "online_hub": {
        "desc": "Free-from-home digital resources — eBooks (BorrowBox), newspapers/"
                "magazines (PressReader), family history (Ancestry). args: {\"topic\": \"\"}",
        "fn": lambda a: ls.online_hub(a.get("topic") or a.get("query"))},
    "libraries_unlocked": {
        "desc": "Extended 8am-8pm self-service access and which branches have it. "
                "args: {\"branch\": \"<optional>\"}",
        "fn": lambda a: ls.libraries_unlocked(a.get("branch") or a.get("query"))},
    "printing_help": {
        "desc": "How to print/photocopy incl. Print Your Way from a phone, + prices. "
                "args: {}",
        "fn": lambda a: ls.printing_help()},
    "membership_help": {
        "desc": "What you need to sign up — digital vs full vs Libraries Unlocked. "
                "args: {\"service\": \"<optional>\"}",
        "fn": lambda a: ls.membership_help(a.get("service") or a.get("query"))},
    "graph_search": {
        "desc": "Match a library by a COMBINATION of features, e.g. 'late-opening "
                "with a café and parking'. args: {\"query\": \"<the request>\"}",
        "fn": lambda a: graph_rag.graph_search(a.get("query", ""))},
}

ROUTER_SYSTEM = (
    "You route a Worcestershire Libraries question to exactly one tool.\nTools:\n"
    + "\n".join(f"- {n}: {t['desc']}" for n, t in TOOLS.items())
    + "\n- none: greeting / off-topic / general 'what can you do'.\n\n"
    "Reply with ONLY JSON: {\"tool\": \"<name>\", \"args\": {...}}. No prose."
)


# --------------------------------------------------------------------------- #
# Routing — LLM first, deterministic keyword fallback always available
# --------------------------------------------------------------------------- #

def keyword_route(q: str) -> tuple[str, dict]:
    t = q.lower()
    feat_hits = sum(1 for w in ("parking", "café", "cafe", "wifi", "wi-fi", "study",
                                "toilet", "computer", "meeting room", "baby")
                    if w in t)
    if re.search(r"\b(print|printing|photocopy|photocopies|scan|copier)\b", t):
        return "printing_help", {}
    if (re.search(r"\b(which|what) librar|librar(y|ies) (with|that has)\b", t)
            or feat_hits >= 2 or "overall" in t):
        return "graph_search", {"query": q}
    if re.search(r"\b(mobile library|mobile van|the van|comes to|visit)\b", t):
        m = re.findall(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)*)\b", q)
        return "mobile_library", {"place": (m[-1] if m else q.split()[-1])}
    if re.search(r"\b(open|opening|hours|close|closing|toilet|parking|address|"
                 r"facilit|where is|near me|study space)\b", t):
        m = re.findall(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)*)\b", q)
        return "find_library", {"name": (m[-1] if m else "")}
    if re.search(r"\b(unlocked|8pm|after hours|out of hours|evening access|"
                 r"open late)\b", t):
        return "libraries_unlocked", {}
    platform = re.search(r"\b(borrowbox|pressreader|ancestry|espacenet|ebsco|oxford|"
                         r"theory test|bfi|cobra|digital library|online (library )?hub)\b", t)
    media = re.search(r"\b(ebooks?|e-books?|audiobooks?|emagazines?)\b", t)
    online_ctx = re.search(r"\b(online|free|digital|from home|at home|on my phone|"
                           r"app|stream(ing)?|download)\b", t)
    if (platform or re.search(r"\bnewspapers?|magazines?\b", t)
            or (media and online_ctx) or re.search(r"read\b.*\bfree", t)):
        return "online_hub", {"topic": q}
    if re.search(r"\b(member|membership|join|library card|sign ?up|what do i need)\b", t):
        return "membership_help", {"service": q}
    if re.search(r"\b(event|events|what'?s on|whats on|activit|class|club|session|"
                 r"group|happening|this week)\b", t):
        return "library_events", {"query": ""}
    if re.search(r"\b(new|newest|latest|recommend|hot take|just in|good read|"
                 r"suggestion)\b", t):
        return "whats_new", {"genre": re.sub(r"\b(new|newest|latest|recommend|any|"
                                             r"good|some|me|a|books?)\b", " ", t).strip()}
    if re.search(r"\b(book|books|read|novel|author|catalog|catalogue|borrow|dvd|"
                 r"have you got|do you have)\b", t):
        q2 = re.sub(r"\b(do you have|have you got|any|the book|a copy of|books?|"
                    r"by|in stock|available)\b", " ", t)
        return "search_catalogue", {"query": q2.strip(" ?.") or q}
    # last resort: a bare title or "<Title> by <Author>" -> catalogue
    if re.search(r"\bby [A-Z]", q) or len(re.findall(r"\b[A-Z][a-z]+", q)) >= 2:
        return "search_catalogue", {"query": q}
    return "none", {}


def route(q: str) -> tuple[str, dict, str, int]:
    t0 = time.time()
    if HF_TOKEN:
        try:
            out = llm([{"role": "system", "content": ROUTER_SYSTEM},
                       {"role": "user", "content": q}],
                      max_tokens=120, temperature=0.0)
            m = re.search(r"\{.*\}", out.choices[0].message.content, re.S)
            if m:
                data = json.loads(m.group(0))
                tool = data.get("tool", "none")
                if tool in TOOLS or tool == "none":
                    return tool, data.get("args", {}) or {}, "llm", _ms(t0)
        except Exception:
            pass
    tool, args = keyword_route(q)
    return tool, args, "keyword", _ms(t0)


def _ms(t0): return int((time.time() - t0) * 1000)


# --------------------------------------------------------------------------- #
# Render live tool results -> markdown (with eligibility woven in)
# --------------------------------------------------------------------------- #

def render_catalogue(r):
    if r.get("error"):
        return f"_Couldn't reach the catalogue: {r['error']}_"
    if not r["items"]:
        return (f"I searched for **{r['query']}** but found nothing — try fewer or "
                f"different words.\n\n🔎 [Search the catalogue]({r['search_url']})")
    out = [f"Found ~**{r.get('total_hint', r['count'])}** matches for **{r['query']}** "
           f"— top {r['count']}:\n"]
    for it in r["items"]:
        meta = " · ".join(b for b in (it["author"], it["format"], it["year"]) if b)
        tag = " _(borrow online)_" if it["digital"] else ""
        link = f" — [details]({it['detail_url']})" if it["detail_url"] else ""
        out.append(f"- {it['icon']} **{it['title']}** — {meta}{tag}{link}")
    out.append(f"\n✅ **To borrow:** {ls.ELIGIBILITY['borrow_physical']} "
               f"eBooks/audio need free digital membership.")
    out.append(f"🔎 [Full results]({r['search_url']})")
    return "\n".join(out)


def render_whats_new(r):
    if not r["items"]:
        return "I couldn't pull new titles just now — try a specific genre."
    out = [f"📚 **Newest '{r['genre']}' in the catalogue:**\n"]
    for it in r["items"]:
        meta = " · ".join(b for b in (it["author"], it["year"]) if b)
        out.append(f"- {it['icon']} **{it['title']}** — {meta}")
    out.append(f"\n🔎 [See more]({r['search_url']})")
    return "\n".join(out)


def render_find_library(r):
    if r.get("error"):
        s = ", ".join(r.get("suggestions", [])[:6])
        return f"{r['error']} Did you mean: {s}?\n\n🔎 [All libraries]({r['page_url']})"
    if "branches" in r:  # list mode
        out = ["📍 **Worcestershire libraries:**\n"]
        for b in r["branches"][:25]:
            out.append(f"- **{b['name']}** — {b['address']}")
        return "\n".join(out)
    badge = "🟢 **Open now**" if r["open_now"] else "🔴 **Closed now**"
    out = [f"📍 **{r['name']}** — {badge} ({r['status']})",
           f"{r['address']}\n",
           f"**Today ({r['today']}):** {r['today_staffed'] or 'see below'}"]
    if r.get("unlocked_today"):
        out.append(f"**Libraries Unlocked self-service:** {r['unlocked_today']}")
    if r.get("facilities"):
        out.append(f"\n**Facilities:** {', '.join(r['facilities'])}")
    out.append(f"\n✅ {ls.ELIGIBILITY['visit']}")
    out.append(f"🔎 [Branch page]({r['page_url']})")
    return "\n".join(out)


def render_mobile(r):
    if r.get("error"):
        s = ", ".join(x.title() for x in (r.get("suggestions") or [])[:8])
        extra = f" Did you mean: {s}?" if s else ""
        return f"{r['error']}{extra}\n\n🔎 [All stops]({r.get('page_url', ls.MOBILE_INDEX)})"
    out = [f"🚐 **Mobile library — {r['village']}**", f"Runs: **{r['date_of_operation']}**\n"]
    for s in r["stops"]:
        out.append(f"- `{s['time']}` — {s['location']}")
    out.append(f"\n✅ {ls.ELIGIBILITY['mobile']}")
    out.append(f"Enquiries: {r['email']} · 🔎 [Timetable]({r['page_url']})")
    return "\n".join(out)


def render_events(r):
    if not r["events"]:
        return f"No matching events found.\n\n🔎 [All events]({r['page_url']})"
    out = [f"📅 **{r['count']} upcoming events:**\n"]
    for e in r["events"]:
        when = " · ".join(b for b in (e.get("next_date"), e["when"], e["time"]) if b)
        loc = f" @ {e['location']}" if e["location"] else ""
        out.append(f"- **[{e['name']}]({e['url']})** — {when}{loc}")
    out.append(f"\n✅ {ls.ELIGIBILITY['events']}\n🔎 [Full listing]({r['page_url']})")
    return "\n".join(out)


def render_online_hub(r):
    out = ["💻 **Free online — with your library card:**\n"]
    for it in r["items"][:5]:
        out.append(f"**{it['name']}** — {it['summary']}")
        if it.get("what_you_need"):
            out.append(f"  - ✅ **What you need:** {it['what_you_need']}")
        if it.get("access"):
            out.append("  - **How:** " + " → ".join(it["access"]))
        if it.get("limits"):
            out.append(f"  - {it['limits']}")
        if it.get("titles"):
            out.append(f"  - _Includes:_ {', '.join(it['titles'][:6])}…")
        out.append("")
    out.append(f"🔎 [Online library hub]({r['page_url']})")
    return "\n".join(out)


def render_unlocked(r):
    out = ["🔓 **Libraries Unlocked** — use the library 8am–8pm, Mon–Sat, even "
           "when it's unstaffed.",
           f"\n✅ **What you need:** {r['what_you_need']}"]
    if r.get("unlocks"):
        out.append(f"\n{r['unlocks']}")
    else:
        out.append(f"\n**Branches:** {', '.join(r['branches'])}.")
    if r.get("branch_match"):
        out.append(f"\n✓ Yes — **{r['branch_match']}** has Libraries Unlocked.")
    elif r.get("branch_match") is None and "branch_match" in r:
        out.append("\nThat branch isn't on the Libraries Unlocked list yet.")
    out.append(f"\n🔎 [Libraries Unlocked]({r['page_url']})")
    return "\n".join(out)


def render_membership(r):
    out = ["🪪 **What you need to sign up:**\n"]
    for tier in r["tiers"]:
        out.append(f"**{tier['tier']}** — {tier['what_you_need']}")
        out.append(f"  - _Unlocks:_ {tier['unlocks']}\n")
    if r.get("need"):
        out.append(f"➡️ For your question: **{r['need']}**")
    out.append(f"\n🔎 [Join the library]({r['page_url']})")
    return "\n".join(out)


def render_printing(r):
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(r["steps"], 1))
    price = "\n".join(f"- {k}: {v}" for k, v in r["pricing"].items())
    return (f"🖨️ **Print Your Way**\n\n{r['summary']}\n\n**Devices:** "
            f"{r['device_requirements']}\n\n**How to print:**\n{steps}\n\n"
            f"**Prices:**\n{price}\n\n🔎 [Printing page]({r['page_url']})")


def render_graph(r):
    _frag = {"caf": "a café", "meeting": "meeting rooms", "wi-fi": "free Wi-Fi",
             "study": "study space", "parking": "parking", "computer": "public computers",
             "toilet": "toilets", "baby": "baby changing", "accessible": "accessible toilet",
             "wheelchair": "wheelchair access", "printing": "printing", "self": "self-service"}
    if r["kind"] == "branch_filter":
        feats = []
        if r["late"]:
            feats.append("open late (8am–8pm)")
        feats += [_frag.get(f, f) for f in r["wanted_facilities"]]
        if r["area"]:
            feats.append(f"in {r['area']}")
        crit = ", ".join(feats) or "your criteria"
        if not r["branches"]:
            return (f"No library currently matches **{crit}** in our data. "
                    "Try fewer features.\n\n🔎 [All libraries](" + r["page_url"] + ")")
        out = [f"🧭 Libraries matching **{crit}**:\n"]
        for b in r["branches"]:
            lu = " · open to 8pm" if b["libraries_unlocked"] else ""
            out.append(f"- **{b['name']}**{lu} — {', '.join(b['facilities'])}")
        return "\n".join(out)
    if r["kind"] == "entity":
        if not r["entities"]:
            return "I couldn't find that in the knowledge graph — try rephrasing."
        out = []
        for e in r["entities"][:3]:
            out.append(f"**{e['label']}** ({e['type']}) — {e.get('summary','')[:160]}")
            if e.get("what_you_need"):
                out.append(f"  - ✅ {e['what_you_need']}")
            rel = ", ".join(f"{x['rel'].lower().replace('_',' ')} {x['label']}"
                            for x in e["related"][:4])
            if rel:
                out.append(f"  - _linked to:_ {rel}")
            if e.get("url"):
                out.append(f"  - 🔎 [more]({e['url']})")
        return "\n".join(out)
    # global
    out = ["🗺️ **Across the whole service:**\n"]
    for c in r["communities"]:
        out.append(f"- **{c['title']}** — {c['report'][:200]}")
    return "\n".join(out)


RENDER = {
    "search_catalogue": render_catalogue, "whats_new": render_whats_new,
    "find_library": render_find_library, "mobile_library": render_mobile,
    "library_events": render_events, "online_hub": render_online_hub,
    "libraries_unlocked": render_unlocked, "membership_help": render_membership,
    "printing_help": render_printing, "graph_search": render_graph,
}


# --------------------------------------------------------------------------- #
# Behaviour-change layer — £-saved value receipt + EAST nudges + chips
# --------------------------------------------------------------------------- #

def value_receipt(tool, raw):
    if tool in ("search_catalogue", "whats_new") and raw.get("items"):
        return "💷 _Borrowing instead of buying ≈ **£9–£20 saved** per title._"
    if tool == "online_hub":
        return ("💷 _Free with your card — a newspaper or eBook subscription is "
                "**~£8–£12/month** you don't pay._")
    if tool == "printing_help":
        return "💷 _Far cheaper than a high-street print shop._"
    return ""


# (EAST: Easy=chips, Attractive=value, Social/Timely=nudge)
NUDGES = {
    "search_catalogue": ("💡 No time to visit? Many titles are free on **BorrowBox** tonight.",
                         ["Is it on BorrowBox?", "Reserve & collect — how?", "Hot takes on new books"]),
    "whats_new": ("💡 Reserve it free and collect at your branch.",
                  ["More like this", "Is it an eBook?", "What's on this week?"]),
    "find_library": ("💡 Want in before/after staffed hours? **Libraries Unlocked** = 8am–8pm.",
                     ["Tell me about Libraries Unlocked", "What's on there?", "How do I join?"]),
    "mobile_library": ("💡 Housebound? The **Home Library Service** brings books to your door.",
                       ["How do I join?", "What's on this week?", "Find my nearest library"]),
    "library_events": ("💡 Most events are free — just turn up.",
                       ["Children's events", "Do I need to book?", "Find my nearest library"]),
    "online_hub": ("💡 It's free with your card — set up tonight from your sofa.",
                   ["How do I sign up?", "What newspapers are there?", "BorrowBox limits"]),
    "libraries_unlocked": ("💡 It's free — just a quick one-off induction.",
                           ["Which branches?", "How do I get the induction?", "What can I do there?"]),
    "printing_help": ("💡 No printer at home? Print from your phone, collect within 24h.",
                      ["Printing prices", "Find my nearest library", "How do I join?"]),
    "membership_help": ("💡 Digital membership is instant — no card needed.",
                        ["Set up digital membership", "What's the difference?", "What can I borrow online?"]),
    "graph_search": ("💡 Tell me what matters (late, café, study space) and I'll match a branch.",
                     ["Late-opening + café", "Find a book", "What's on this week?"]),
}
HELP_CHIPS = ["Do you have Harry Potter?", "Mobile library near me",
              "What's on this week?", "How do I print from my phone?"]


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #

SYNTH_SYSTEM = (
    "You are the Worcestershire Libraries assistant. Answer ONLY from the LIVE "
    "DATA provided — never invent titles, times, prices, stops or facilities. Warm, "
    "concise, British English. Keep the markdown links and the ✅/🔎 lines from the "
    "data. If the data doesn't answer it, say so and point to the source link.")


def synthesize_stream(question, rendered):
    if not HF_TOKEN:
        yield rendered
        return
    try:
        stream = llm([{"role": "system", "content": SYNTH_SYSTEM},
                      {"role": "user", "content": f"Question: {question}\n\n"
                       f"LIVE DATA:\n{rendered}"}],
                     max_tokens=650, temperature=0.3, stream=True)
        acc = ""
        for chunk in stream:
            d = chunk.choices[0].delta.content or ""
            if d:
                acc += d
                yield acc
        if not acc.strip():
            yield rendered
    except Exception:
        yield rendered


HELP = (
    "👋 I'm the **Worcestershire Libraries assistant**. I check the council site "
    "and catalogue *live* and can help you:\n\n"
    "- 📚 **Find a book / eBook / audiobook**\n"
    "- 📍 **Branch hours, 'open now?', toilets, parking**\n"
    "- 🚐 **Mobile library** times for your village\n"
    "- 📅 **What's on** this week\n"
    "- 💻 **Free online** — newspapers, magazines, family history\n"
    "- 🖨️ **Printing** from your phone\n\n"
    "_Every answer is checked live against official Worcestershire County "
    "Council sources._")


# --------------------------------------------------------------------------- #
# Chat handler — yields (answer_text, chips_or_None)
# --------------------------------------------------------------------------- #

def respond(message, history):
    message = (message or "").strip()
    if not message:
        yield HELP, HELP_CHIPS
        return

    tr = Trace(message, MODEL_ID)
    tool, args, how, rms = route(message)
    tr.set_route(tool, args, how, rms)

    if tool == "none":
        tr.finish(HELP, []).save()
        yield HELP, HELP_CHIPS
        return

    t1 = time.time()
    try:
        raw = TOOLS[tool]["fn"](args)
    except Exception as e:
        tr.step("tool_call", name=tool, ok=False, error=str(e)).finish("", []).save()
        yield (f"Sorry — I couldn't reach the library source just now. "
               f"Please try again in a moment."), None
        return
    rendered = RENDER[tool](raw)
    source = raw.get("page_url") or raw.get("search_url") or ""
    tr.step("tool_call", name=tool, source=source, ms=_ms(t1), ok=True)

    t2 = time.time()
    answer = ""
    for partial in synthesize_stream(message, rendered):
        answer = partial
        yield answer, None
    tr.step("synthesis", model=MODEL_ID, ms=_ms(t2))

    # behaviour-change extras + provenance + open trace
    value = value_receipt(tool, raw)
    nudge, chips = NUDGES.get(tool, ("", []))
    checked = raw.get("checked", "")
    footer = (f"\n\n<sub>🔎 Checked **live**"
              + (f" · {checked}" if checked else "")
              + f" · tool `{tool}` ({how})"
              + (f" · [source]({source})" if source else "") + "</sub>")
    tr.finish(answer, [source]).save()

    final = answer
    if value:
        final += f"\n\n{value}"
    if nudge:
        final += f"\n\n{nudge}"
    final += footer + tr.to_markdown()
    yield final, chips


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

CSS = """
:root { --wcc:#0a7d78; --wcc-dark:#075a56; }
.gradio-container { max-width: 940px !important; margin: auto !important;
  font-family:'Segoe UI', system-ui, sans-serif; }
#hero { background:linear-gradient(135deg,var(--wcc),var(--wcc-dark)); color:#fff;
  padding:22px 26px; border-radius:16px; margin-bottom:12px;
  box-shadow:0 6px 20px rgba(7,90,86,.25); }
#hero h1 { margin:0; font-size:1.5rem; }
#hero p { margin:.4rem 0 0; opacity:.92; font-size:.93rem; }
#hero .live { display:inline-block; background:#fff; color:var(--wcc-dark);
  font-weight:700; font-size:.7rem; padding:2px 9px; border-radius:999px;
  margin-bottom:8px; text-transform:uppercase; letter-spacing:.5px; }
footer { visibility:hidden; }
.prose a { color:var(--wcc-dark); font-weight:600; }
"""


def build_demo():
    with gr.Blocks(title="Worcestershire Libraries — Live Assistant") as demo:
        gr.HTML("<div id='hero'><span class='live'>● live data</span>"
                "<h1>Worcestershire Libraries — Live Assistant</h1>"
                "<p>Books, mobile library, events, printing and what's free online — "
                "answered live from the council site & catalogue, with exactly what "
                "you need to sign up.</p></div>")

        chat = gr.Chatbot(height=460, show_label=False,
                          placeholder="📚 Ask me anything about your local library…",
                          elem_classes=["prose"])
        with gr.Row():
            box = gr.Textbox(placeholder="e.g. When does the mobile library visit Abberley?",
                             show_label=False, scale=8, autofocus=True)
            send = gr.Button("Ask", variant="primary", scale=1)
        with gr.Row():
            chips = [gr.Button(visible=False, size="sm", variant="secondary")
                     for _ in range(3)]

        gr.Examples(
            ["Do you have Harry Potter audiobooks?",
             "Is Malvern library open now?",
             "A late-opening library with a café and meeting rooms",
             "When does the mobile library visit Abberley?",
             "Can I read newspapers for free?",
             "What do I need to sign up?"],
            inputs=box, label="Try one")

        if not HF_TOKEN:
            gr.Markdown("> ⚠️ No `HF_TOKEN` set — **no-LLM mode**: you get the raw "
                        "live data (still fully working). Add an `HF_TOKEN` secret "
                        "for conversational phrasing.")

        def hide3():
            return tuple(gr.update(visible=False) for _ in range(3))

        def show3(sugg):
            sugg = (sugg or []) + ["", "", ""]
            return tuple(gr.update(value=sugg[i], visible=bool(sugg[i]))
                         for i in range(3))

        def user_turn(msg, hist):
            if not (msg or "").strip():
                return "", hist or [], *hide3()
            return "", (hist or []) + [{"role": "user", "content": msg}], *hide3()

        def chip_turn(label, hist):
            return "", (hist or []) + [{"role": "user", "content": label}], *hide3()

        def msg_text(content):
            # Gradio 6 Chatbot returns content as a list of blocks
            # ([{"text": ..., "type": "text"}]) rather than a plain string.
            if isinstance(content, list):
                return " ".join(b.get("text", "") for b in content
                                if isinstance(b, dict)).strip()
            return content or ""

        def bot_turn(hist):
            if not hist or hist[-1]["role"] != "user":
                yield hist, *hide3()
                return
            msg = msg_text(hist[-1]["content"])
            hist = hist + [{"role": "assistant", "content": ""}]
            final_chips = []
            for text, ch in respond(msg, hist[:-1]):
                hist[-1]["content"] = text
                if ch is not None:
                    final_chips = ch
                yield hist, gr.update(), gr.update(), gr.update()
            yield hist, *show3(final_chips)

        outs = [chat, *chips]
        box.submit(user_turn, [box, chat], [box, chat, *chips], queue=False).then(
            bot_turn, chat, outs)
        send.click(user_turn, [box, chat], [box, chat, *chips], queue=False).then(
            bot_turn, chat, outs)
        for c in chips:
            c.click(chip_turn, [c, chat], [box, chat, *chips], queue=False).then(
                bot_turn, chat, outs)

    return demo


if __name__ == "__main__":
    build_demo().queue().launch(server_name="0.0.0.0", server_port=7860,
                                theme=gr.themes.Soft(primary_hue="teal"),
                                css=CSS)
