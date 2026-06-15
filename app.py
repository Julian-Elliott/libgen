"""
Worcestershire Libraries — Live Assistant  (Build Small Hackathon)

A small-model (<=32B) civic agent that answers real questions about
Worcestershire Libraries from official data at EVERY granularity:
service pages -> every page of the Hive site -> catalogue items -> the
individual copies on a branch's shelf.

  • Live tools  — catalogue, mobile library, events, branch hours/facilities
  • where_to_get — the full "how do I actually get this title" journey:
    on-shelf copy at a named branch / free reservation / BorrowBox tonight
  • hive_info   — The Hive (Worcester) page-by-page: archives & archaeology,
    800+ study spaces, room hire, BIPC, Youth Hub, 8:30am–10pm every day
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
        "desc": "Browse/search what the library holds (book, eBook, audiobook, DVD). "
                "args: {\"query\": \"<title / author / subject>\"}",
        "fn": lambda a: ls.search_catalogue(a.get("query", ""), limit=6)},
    "where_to_get": {
        "desc": "User wants to actually GET/borrow/obtain a specific title — gives "
                "the exact route: which branch has it on the shelf NOW (copy-level), "
                "free reservation steps, or borrow tonight on BorrowBox. "
                "args: {\"query\": \"<title / author>\"}",
        "fn": lambda a: ls.where_to_get(a.get("query") or a.get("title", ""))},
    "hive_info": {
        "desc": "The Hive — Worcester's library (joint university+public): hours "
                "(8:30am-10pm daily), Explore the Past archives & archaeology, study "
                "spaces, room hire, café, getting there, children's library, business "
                "support. Page-level detail. args: {\"topic\": \"<optional topic>\"}",
        "fn": lambda a: ls.hive_info(a.get("topic") or a.get("query"))},
    "whats_new": {
        "desc": "Newest titles in a genre, for fun recommendations. "
                "args: {\"genre\": \"<genre/topic>\"}",
        "fn": lambda a: ls.whats_new(a.get("genre") or a.get("query"))},
    "find_library": {
        "desc": "A branch's opening hours ('open now?', 'open tomorrow?'), address "
                "and facilities (toilets, parking, café, study space). Include the "
                "day if the user names one. args: {\"name\": \"<branch>\", "
                "\"day\": \"today|tomorrow|<weekday>\"}",
        "fn": lambda a: ls.find_library(a.get("name") or a.get("query"),
                                        when=a.get("day") or a.get("when"))},
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
    "account_and_loans": {
        "desc": "Renew loans, check/pay fines, make reservations, access the online "
                "account, borrowing periods & limits, returning items, lost cards, "
                "PIN reset, job clubs, and the Library Service at Home (housebound). "
                "args: {\"query\": \"<optional topic>\"}",
        "fn": lambda a: ls.account_and_loans(a.get("query") or a.get("topic"))},
    "children_services": {
        "desc": "Children's library activities — Storytime, Rhymetime, the Summer "
                "Reading Challenge, holiday events and children's books. args: {}",
        "fn": lambda a: ls.children_services()},
}

ROUTER_SYSTEM = (
    "You route a Worcestershire Libraries question to exactly one tool.\nTools:\n"
    + "\n".join(f"- {n}: {t['desc']}" for n, t in TOOLS.items())
    + "\n- none: greeting / off-topic / general 'what can you do'.\n\n"
    "If conversation context is provided, resolve references like 'there', 'it' "
    "or 'that one' from it (e.g. 'what's on there?' after a Malvern answer -> "
    "library_events with Malvern).\n"
    "Reply with ONLY JSON: {\"tool\": \"<name>\", \"args\": {...}}. No prose."
)


def _content_text(content):
    # Gradio 6 Chatbot content can be a list of blocks rather than a string.
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict)).strip()
    return content or ""


def _history_text(history, limit=4):
    lines = []
    for m in (history or [])[-limit:]:
        c = _content_text(m.get("content"))
        if c:
            lines.append(f"{m.get('role', 'user')}: {c[:200]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Routing — LLM first, deterministic keyword fallback always available
# --------------------------------------------------------------------------- #

_QWORDS = {"what", "whats", "when", "where", "which", "who", "how", "is", "are",
           "do", "does", "can", "tell", "any", "the", "this", "a", "i", "my"}

# Words to drop when pulling a branch/village name out of a question, so we
# don't latch onto "What" or "library" instead of "bromsgrove".
_PLACE_STOP = _QWORDS | {
    "time", "times", "open", "opening", "opens", "close", "closing", "closed",
    "hours", "hour", "library", "libraries", "branch", "today", "tomorrow",
    "tonight", "now", "morning", "afternoon", "evening", "on", "at", "in", "to",
    "for", "of", "me", "you", "it", "near", "address", "parking", "toilet",
    "toilets", "facilities", "facility", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "weekend", "week", "day", "next",
    "mobile", "van", "visit", "visits", "comes", "service", "opened",
    "wi-fi", "wifi", "wi", "fi", "wireless", "internet", "broadband",
}


def _place_from(q: str) -> str:
    """Branch/village name from a question — case-insensitive, stopword-filtered."""
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", q)
    return " ".join(w for w in toks if w.lower() not in _PLACE_STOP).strip()


def keyword_route(q: str) -> tuple[str, dict]:
    t = q.lower()
    feat_hits = sum(1 for w in ("parking", "café", "cafe", "wifi", "wi-fi", "study",
                                "toilet", "computer", "meeting room", "baby")
                    if w in t)
    if (re.search(r"\b(print|printing|photocopy|photocopies|scan|copier)\b", t)
            and not re.search(r"\blarge.?print\b|\bbig.?print\b", t)):
        return "printing_help", {}
    # Expired card / membership renewal — before generic renew check
    if (re.search(r"\b(library )?card (has |is |)(expired|lapsed|out of date)\b|"
                  r"\bexpired (library )?card\b|"
                  r"\b(my |the )?(library )?(membership|member) (has |is |)(expired|lapsed)\b", t)
            or (re.search(r"\brenew (my |the )?(library )?(card|membership)\b", t)
                and not re.search(r"\bbooks?\b|\bitems?\b|\bloans?\b", t))):
        return "account_and_loans", {"query": "card expired renew membership"}
    # Account self-service: renewals, fines, reservations, home library
    if re.search(r"\b(renew|renewal|due date)\b|"
                 r"\bextend(ing)? (a |my |the |)?(loan|book|borrow|borrowing|item|items|them|it)\b|"
                 r"\bcan (i|we) (renew|extend)\b", t):
        return "account_and_loans", {"query": q}
    if re.search(r"\b(how (long|many)\b[^?]{0,40}\b(borrow|loan|keep|renew|take out|"
                 r"out at once)|loan period|borrowing limit|how many (books|items) can i)\b", t):
        return "account_and_loans", {"query": "loan limits how long how many"}
    # Lost / stolen library card — dedicated focus (before fines catch-all)
    if re.search(r"\blost (my |a |the )?(library )?card\b|"
                 r"(my |a )?(library )?card (is |was |has been )?(lost|stolen)\b|"
                 r"(stolen|stole).{0,20}(library )?card\b|"
                 r"\bmy card.{0,15}(stolen|stolen|missing)\b|"
                 r"replace (a |my |the )?(library )?card\b", t):
        return "account_and_loans", {"query": "lost stolen library card replacement"}
    if re.search(r"\b(fine|fines|overdue|late fee|pay (a |my )?(fee|fine)|"
                 r"fees?[ -]and[ -]charges?|lost (a |my )?book)\b", t):
        return "account_and_loans", {"query": q}
    if re.search(r"\b(my (library )?account|online account|(sign|log) ?in(to)?|"
                 r"my (library )?pin|forgot (my )?(pin|password)|"
                 r"library card (number|login))\b", t):
        return "account_and_loans", {"query": q}
    if "cancel" in t and re.search(r"reserv|\bhold\b", t):
        return "account_and_loans", {"query": "cancel reservation"}
    if (re.search(r"\bhow (do|can) i (reserve|place a hold|request)|"
                  r"(make|place) a (reserve|reservation|hold)\b", t)
            and not re.search(r"\b(room|space|study|seat|hive)\b", t)):
        return "account_and_loans", {"query": q}
    if re.search(r"\b(home library|library (service )?(at home|at-home|home)|"
                 r"housebound|books? delivered|deliver (books?|the library)|"
                 r"can.{0,12}t get to (the |a )?librar)\b", t):
        return "account_and_loans", {"query": q}
    if re.search(r"\b(update|change|amend|correct) (my )?(address|detail|contact|"
                 r"email|phone|mobile|name)\b|"
                 r"\bi'?ve? moved\b|"
                 r"\bmy (address|detail) (has |have |is |)(changed|wrong|moved|updated)\b", t):
        return "account_and_loans", {"query": "update my account details address"}
    if re.search(r"\breturn(ing)? (a |my |the )?(book|item|loan)\b|"
                 r"how (do|to|can) (i )?return\b", t):
        return "account_and_loans", {"query": "return book"}
    if (re.search(r"\b(lost|damaged|damage).{0,20}\b(book|item|dvd)\b|"
                  r"\b(book|item|dvd).{0,15}(lost|damaged)\b", t)
            and "card" not in t):
        return "account_and_loans", {"query": "lost damaged book item"}
    if re.search(r"\b(ask for a book|book recommendation|personalised (book|read)|"
                 r"suggest (me )?a book|what should i read|choose (me )?a book|"
                 r"pick (me )?a book|recommend (me )?a book|reading suggestion)\b", t):
        return "account_and_loans", {"query": "ask for a book recommendation"}
    if re.search(r"\bbook (a )?computer\b|use (a |the )?computer|pc session|"
                 r"computer session|book a pc\b|"
                 r"computers?.{0,30}librar|(library|libraries).{0,15}computers?\b", t):
        return "account_and_loans", {"query": "book a computer"}
    # Self-service kiosk — how to check out / return without staff
    if re.search(r"\bself[- ]?service (kiosk|machine|terminal)?\b|"
                 r"\bself[- ]?checkout\b|\bkiosk\b|"
                 r"\b(check (out|in)).{0,20}(myself|machine|kiosk|without staff)\b|"
                 r"\buse (the |a )?self[- ]?service\b", t):
        return "account_and_loans", {"query": "self service kiosk checkout borrow return"}
    if re.search(r"\bvolunteer(ing)?\b|work experience (at|in) (a |the )?librar\b", t):
        return "account_and_loans", {"query": "volunteering"}
    if re.search(r"\bdigital (skills?|inclusion|champion)\b|learn my way\b|"
                 r"get online\b|learn to use (a )?computer\b", t):
        return "account_and_loans", {"query": "digital skills"}
    if re.search(r"\breading well\b|mental health books?|wellbeing books?|"
                 r"books? (for|about) (mental health|wellbeing)\b|"
                 r"(library|have|borrow).{0,20}(mental health|wellbeing)\b", t):
        return "account_and_loans", {"query": "reading well"}
    if re.search(r"\bwarm (space|welcome|place)\b|somewhere warm\b", t):
        return "account_and_loans", {"query": "warm space"}
    if re.search(r"\bjob club\b|job[- ]?seeking|job search|cv (help|writing|advice)|"
                 r"employment support|find work|looking for work|interview (prep|help)\b", t):
        return "account_and_loans", {"query": "job club cv employment"}
    # Book clubs / reading groups
    if re.search(r"\bbook (club|group)\b|reading (group|club|circle)\b|"
                 r"\breaders'? (group|circle|club)\b", t):
        return "account_and_loans", {"query": "book club reading group"}
    # Adult learning / skills courses
    if re.search(r"\badult (learn|class|course|train|skill)|"
                 r"\blearn\w* (course|class|skill|program)|\bit (course|class)|"
                 r"\bcomputer (course|class|lesson)|\bupskill|\bfunctional skills?\b|"
                 r"\blearn to (read|write|type)\b", t):
        return "account_and_loans", {"query": "adult learning course"}
    # Book donations
    if re.search(r"\bdonate (books?|items?) (to|at|for) (\w+ )?librar|"
                 r"\bbook donation\b|give (books?|items?) (to|in to) (\w+ )?librar|"
                 r"\bdrop off books?\b|second[- ]?hand books? (to|for|at) (\w+ )?librar|"
                 r"\bbring books? (in|to) (the |a )?librar", t):
        return "account_and_loans", {"query": "donate books"}
    # Early years / pre-school — check BEFORE school-visits to avoid misrouting
    if re.search(r"\bearly years?\b|get school ready\b|"
                 r"\bpre[- ]?school (library|books?|session|activit)\b|"
                 r"\bschool ready\b", t):
        return "children_services", {}
    # School and group visits to libraries
    if re.search(r"\bschool (visit|trip|group|class|tour)\b|"
                 r"\b(teachers?|class(es)?) .{0,45}librar|"
                 r"\bgroup (visit|booking) .{0,40}librar|"
                 r"\barrange .{0,20}(school|class|group).{0,25}librar", t):
        return "account_and_loans", {"query": "school visit group visit"}
    # Children's services — Storytime, Rhymetime, Summer Reading Challenge, early years (not Hive-specific)
    if (re.search(r"\b(storytime|story time|rhymetime|rhyme time|bounce and rhyme|"
                  r"summer reading|reading challenge|summer challenge|"
                  r"children'?s? (librar|service|activit|"
                  r"event|book|section)|kids'? (activit|event|club|book)|toddler|"
                  r"under[- ]?5s?|baby (rhyme|group)|for (kids|children|toddlers))", t)
            and not re.search(r"\bhive\b", t)):
        return "children_services", {}
    # Business support / BIPC — based at The Hive
    if re.search(r"\bbipc\b|business (advice|support|centre|center)|"
                 r"intellectual property|start (a |my )?business\b", t):
        return "hive_info", {"topic": "business"}
    # Study / quiet space — match a branch by facility via the graph
    if (re.search(r"\b(where can i study|somewhere to study|a (quiet|study) "
                  r"(space|spot|area|room)|need (a )?(quiet|study) (space|spot|room|area)|"
                  r"quiet (place|spot|space) to (study|work|read))\b", t)
            and not re.search(r"\bhive\b", t)):
        return "graph_search", {"query": q}
    # Accessibility — match an accessible branch via the graph
    if (re.search(r"\b(wheelchair|accessible|accessibility|disabled|disability|"
                  r"step[- ]free|hearing loop|induction loop|dyslexia|"
                  r"visual impair|sight impair|partially sighted)\b", t)
            and re.search(r"\blibrar", t)):
        return "graph_search", {"query": q}
    # Youth Hub / Careers Hub at The Hive — route even without 'hive' keyword
    if re.search(r"\b(youth hub|careers hub|career hub|careers centre|careers center)\b|"
                 r"\bhive.{0,25}(career|youth|young people)\b|"
                 r"\b(career|youth).{0,25}\bhive\b", t):
        return "hive_info", {"topic": "youth hub careers"}
    # Non-Hive meeting room hire — Hive-specific queries fall through to hive_info below
    if (re.search(r"\bhire (a |the )?(meeting )?room\b|(meeting )?room (for )?hire\b|"
                  r"book a (meeting )?room\b|meeting room hire\b", t)
            and not re.search(r"\bhive\b", t)):
        return "account_and_loans", {"query": "room hire meeting room"}
    # The Hive / its extended offer (archives, archaeology, study spaces, Worcester city)
    if re.search(r"\b(the )?hive\b|archiv|archaeolog|explore the past|"
                 r"worcester city librar|book a (space|study)\b", t):
        topic = re.sub(r"\b(the|hive|at|in|about|tell|me|what|can|i|do|you|"
                       r"know|is|are|there)\b", " ", t)
        return "hive_info", {"topic": topic.strip(" ?.") or None}
    # "how do I actually GET it" — the full per-copy journey
    if (re.search(r"\b(where|how) (can|do|could|would) i (get|borrow|find|read|"
                  r"listen to)\b|\bget hold of\b|\bi want to (read|borrow)\b|"
                  r"\bis .{3,60} (available|in stock|on the shelf)\b|"
                  r"\b(nearest|closest) copy\b", t)
            and not re.search(r"\bcard|member|join|sign ?up\b", t)):
        q2 = re.sub(r"\b(where|how|can|do|could|would|i|get|borrow|find|read|"
                    r"listen|to|hold|of|want|is|available|in stock|on the shelf|"
                    r"a copy of|the book|nearest|closest|copy)\b", " ", t)
        q2 = q2.strip(" ?.")
        if q2:
            return "where_to_get", {"query": q2}
    if (re.search(r"\b(which|what) librar|librar(y|ies) (with|that has)\b", t)
            or feat_hits >= 2 or "overall" in t):
        return "graph_search", {"query": q}
    if re.search(r"\b(mobile library|mobile van|the van|comes to|visit)\b", t):
        place = _place_from(q)
        return "mobile_library", {"place": place or q.split()[-1]}
    # Wi-Fi connection instructions — "how to connect" queries; "is there wifi at X" still → find_library
    if re.search(r"\bwi-?fi\b|\bwireless internet\b", t) and \
       re.search(r"\b(connect(ing)?|connection|password|log ?in|sign ?in|"
                 r"how (do|can|to)|guest|setup|set up|access)\b", t):
        return "account_and_loans", {"query": "wifi how to connect password access"}
    if re.search(r"\b(open|opening|hours|close|closing|toilet|parking|address|"
                 r"facilit|where is|near me|study space|wi-?fi|wireless|phone number|"
                 r"telephone|contact (details?|number)|email address|"
                 r"bank holiday|public holiday|christmas|easter|good friday|new year'?s)\b", t):
        return "find_library", {"name": _place_from(q), "when": q}
    if re.search(r"\b(unlocked|8pm|after hours|after work|out of hours|"
                 r"evening access|open late|get in (early|late))\b", t):
        return "libraries_unlocked", {}
    # Accessible reading formats — general (Braille, print disability, accessible editions)
    if re.search(r"\baccessible? (read(?:s|ing)?|books?|formats?|edition)\b|"
                 r"\baccessible reading\b|"
                 r"\bbraille\b|\bprint.?disabilit\b|"
                 r"\b(visual|sight).{0,12}impair.{0,25}\b(books?|reading|formats?)\b", t):
        return "account_and_loans", {"query": "accessible formats reading"}
    # Inter-library loans — items not held in the Worcestershire network
    if re.search(r"\binter[- ]?library (loan|borrow|request|service)?\b|"
                 r"\bill (request|loan|service)?\b|"
                 r"\b(borrow|get|obtain|request|order).{0,35}(another|different|other) librar|"
                 r"\bfrom (another|a different|other) librar", t):
        return "account_and_loans", {"query": "inter-library loan another library service"}
    # Talking books / accessible audio — route to BorrowBox via online_hub
    if re.search(r"\b(talking books?|daisy (format|books?|reader)\b|rnib\b|"
                 r"listening books? (service|online)?\b|"
                 r"audiobooks?.{0,25}(visual|blind|sight|impair))", t):
        return "online_hub", {"topic": "audiobooks borrowbox accessible reading"}
    # Large print — route straight to catalogue with format hint
    if re.search(r"\blarge[- ]?print\b|big[- ]?print books?\b", t):
        title = re.sub(r"\b(large[- ]?print|big[- ]?print|books?|do you have|"
                       r"are there|have you got|any|available|at (the |a )?librar\w*)\b",
                       " ", t).strip(" ?.")
        return "search_catalogue", {"query": f"large print {title}".strip()}
    platform = re.search(r"\b(borrowbox|pressreader|ancestry|espacenet|ebsco|oxford|oed|"
                         r"theory test|bfi|cobra|digital library|online (library )?hub|"
                         r"encyclopaedia|encyclopedia|family (history|tree)|"
                         r"genealog(y|ical)|patents?|national biography|census|"
                         r"consumer advice|product review|best buy)\b", t)
    media = re.search(r"\b(ebooks?|e-books?|audiobooks?|emagazines?)\b", t)
    online_ctx = re.search(r"\b(online|free|digital|from home|at home|on my phone|"
                           r"app|stream(ing)?|download)\b", t)
    if (platform or re.search(r"\bnewspapers?|magazines?\b", t)
            or (media and online_ctx) or re.search(r"read\b.*\bfree", t)):
        return "online_hub", {"topic": q}
    if re.search(r"\b(member|membership|join|library card|sign ?up|what do i need)\b", t):
        return "membership_help", {"service": q}
    # Dementia-friendly / memory activities — route to events with a focused query
    if re.search(r"\bdementia\b|dementia[- ]?friendly\b|memory (cafe|group|session|club|"
                 r"activities?|support)\b|alzheimer\b|forget(ting|fulness)\b.{0,30}activit", t):
        return "library_events", {"query": "dementia memory"}
    if re.search(r"\b(event|events|what'?s on|whats on|activit|class|club|session|"
                 r"group|happening|this week)\b", t):
        m = [w for w in re.findall(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)*)\b", q)
             if w.lower() not in _QWORDS]
        return "library_events", {"query": (m[-1] if m else "")}
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


def route(q: str, history=None) -> tuple[str, dict, str, int]:
    t0 = time.time()
    if HF_TOKEN:
        try:
            user = q
            ctx = _history_text(history)
            if ctx:
                user = f"Conversation so far:\n{ctx}\n\nRoute this latest question: {q}"
            out = llm([{"role": "system", "content": ROUTER_SYSTEM},
                       {"role": "user", "content": user}],
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
                f"different words.\n\n💡 Not in stock? [Ask us to buy it]"
                f"({ls.ASK_FOR_A_BOOK_URL}) — staff review suggestions.\n\n"
                f"🔎 [Search the catalogue]({r['search_url']})")
    out = [f"Found ~**{r.get('total_hint', r['count'])}** matches for **{r['query']}** "
           f"— top {r['count']}:\n"]
    for it in r["items"]:
        meta = " · ".join(b for b in (it["author"], it["format"], it["year"]) if b)
        tag = " _(borrow online)_" if it["digital"] else ""
        link = f" — [details]({it['detail_url']})" if it["detail_url"] else ""
        out.append(f"- {it['icon']} **{it['title']}** — {meta}{tag}{link}")
    out.append(f"\n✅ **To borrow:** {ls.ELIGIBILITY['borrow_physical']} "
               f"eBooks/audio need free [digital membership]({ls.JOIN_URL}).")
    out.append(f"🔎 [Full results]({r['search_url']})")
    return "\n".join(out)


def render_whats_new(r):
    if not r["items"]:
        return "I couldn't pull new titles just now — try a specific genre."
    out = [f"📚 **Newest '{r['genre']}' in the catalogue:**\n"]
    for it in r["items"]:
        meta = " · ".join(b for b in (it["author"], it["year"]) if b)
        out.append(f"- {it['icon']} **{it['title']}** — {meta}")
    out.append(f"\n✅ **To borrow:** {ls.ELIGIBILITY['borrow_physical']}")
    out.append(f"🔎 [See more]({r['search_url']})")
    return "\n".join(out)


def render_find_library(r):
    if r.get("error"):
        s = ", ".join(r.get("suggestions", [])[:6])
        return f"{r['error']} Did you mean: {s}?\n\n🔎 [All libraries]({r['page_url']})"
    if "branches" in r:  # list mode
        out = ["📍 **Worcestershire libraries:**\n"]
        for b in r["branches"][:25]:
            name = f"[{b['name']}]({b['url']})" if b.get("url") else b["name"]
            out.append(f"- **{name}** — {b['address']}")
        return "\n".join(out)
    day_label = r.get("day_label", f"today ({r.get('today','')})")
    heading = day_label[0].upper() + day_label[1:]
    if r.get("is_today", True):
        badge = "🟢 **Open now**" if r["open_now"] else "🔴 **Closed now**"
        head = f"📍 **{r['name']}** — {badge} ({r['status']})"
    else:
        head = f"📍 **{r['name']}** — **{heading}:** {r['status']}"
    closed_txt = ("closed (staffed) — but open for self-service below"
                  if r.get("unlocked") else "closed")
    out = [head, f"{r['address']}\n",
           f"**{heading}:** {r.get('staffed') or closed_txt}"]
    if r.get("unlocked"):
        out.append(f"**Libraries Unlocked self-service:** {r['unlocked']} — "
                   f"{ls.ELIGIBILITY['unlocked']} "
                   f"[How to get access]({ls.UNLOCKED_URL})")
    if r.get("facilities"):
        out.append(f"\n**Facilities:** {', '.join(r['facilities'])}")
    out.append(f"\n✅ {ls.ELIGIBILITY['visit']}")
    out.append(f"📅 _Bank holidays & special closures: [2026 closing dates]({ls.CLOSING_DATES_URL})_")
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
        name = f"[{it['name']}]({it['url']})" if it.get("url") else it["name"]
        out.append(f"**{name}** — {it['summary']}")
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
           f"\n✅ **What you need:** {r['what_you_need']} "
           f"Not a member yet? [Join online]({ls.JOIN_URL}) first."]
    if r.get("unlocks"):
        out.append(f"\n{r['unlocks']}")
    else:
        out.append(f"\n**Branches:** {', '.join(r['branches'])}.")
    if r.get("branch_match"):
        out.append(f"\n✓ Yes — **{r['branch_match']}** has Libraries Unlocked.")
    elif r.get("branch_match") is None and "branch_match" in r:
        out.append("\nThat branch isn't on the Libraries Unlocked list yet.")
    if r.get("get_started"):
        out.append("\n**🚀 How to get started:**")
        out += [f"{i}. {s}" for i, s in enumerate(r["get_started"], 1)]
    out.append(f"\n🔎 [Libraries Unlocked]({r['page_url']})")
    return "\n".join(out)


def render_membership(r):
    out = ["🪪 **What you need to sign up:**\n"]
    for tier in r["tiers"]:
        name = (f"[{tier['tier']}]({tier['url']})" if tier.get("url")
                else tier["tier"])
        out.append(f"**{name}** — {tier['what_you_need']}")
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
            name = f"[{b['name']}]({b['url']})" if b.get("url") else b["name"]
            addr = f" — {b['address']}" if b.get("address") else ""
            out.append(f"- **{name}**{lu}{addr} — {', '.join(b['facilities'])}")
        out.append("\n💬 Ask me _\"is it open now?\"_ about any of these for "
                   "live hours.")
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


def render_where_to_get(r):
    if not r.get("found"):
        rt = r["routes"][0]
        return (f"I couldn't find **{r['query']}** in the catalogue. {rt['advice']}\n\n"
                f"🔎 [Try the catalogue yourself]({r['search_url']})")
    out = [f"**{r['best_title']}**" + (f" — {r['author']}" if r["author"] else "")
           + f" · held as: {', '.join(r['formats_held'])}\n"]
    for rt in r["routes"]:
        if rt["route"] == "digital":
            link = ("[Open it in BorrowBox]" if rt["direct_link"]
                    else "[Open BorrowBox (Worcestershire)]")
            out.append(f"💻 **Borrow the {rt['format']} free tonight** — "
                       f"{link}({rt['url']})")
            out += [f"  {i}. {s}" for i, s in enumerate(rt["steps"], 1)]
            out.append(f"  - ✅ {rt['need']}")
        elif rt["route"] == "shelf":
            out.append(f"📚 **{rt['format']} — on the shelf right now at:**")
            for c in rt["copies"]:
                call = f" · shelf mark `{c['call_number']}`" if c["call_number"] else ""
                out.append(f"  - **{c['branch']}**{call} — _{c['status']}_")
            out.append(f"  - ✅ {rt['need']}")
            out.append(f"  - 🔎 [Item page / all copies]({rt['url']})")
        elif rt["route"] == "reserve":
            out.append(f"🔖 **{rt['format']} — no copy on a shelf just now: "
                       f"reserve it free.**")
            if rt.get("copies"):
                spots = "; ".join(f"{c['branch']}: {c['status']}"
                                  for c in rt["copies"][:5])
                out.append(f"  - Current copies — {spots}")
            elif not rt.get("availability_parsed"):
                out.append("  - _(I couldn't read live copy status this time — "
                           "the item page below shows it.)_")
            out += [f"  {i}. {s}" for i, s in enumerate(rt["steps"], 1)]
            out.append(f"  - ✅ {rt['need']}")
            out.append(f"  - 🔎 [Reserve here]({rt['url']})")
        else:
            out.append(f"✋ {rt.get('advice', '')}")
    if r.get("other_matches"):
        out.append(f"\n_Similar in the catalogue:_ {' · '.join(r['other_matches'])}")
    out.append(f"\n🔎 [Full search results]({r['search_url']})")
    return "\n".join(out)


def render_hive(r):
    hours = r.get("opening_hours", "")
    out = [f"🐝 **{r['name']}** — Worcester's library, run jointly by the "
           f"University of Worcester and Worcestershire County Council."]
    if hours:
        out.append(f"🕗 **{hours}**")
    if r.get("kind") == "overview":
        out.append(f"{r.get('address', '')}\n")
        out.append("**More than a branch:**")
        for c in r.get("capabilities", [])[:9]:
            if isinstance(c, dict):
                src = f" — [source]({c['source']})" if c.get("source") else ""
                out.append(f"- **{c.get('capability', '')}** — "
                           f"{c.get('detail', '')[:160]}{src}")
        out.append(f"\n✅ {ls.ELIGIBILITY['hive_visit']}")
    else:
        for p in r.get("pages", []):
            out.append(f"\n**[{p['title']}]({p['url']})** — {p['summary'][:180]}")
            for o in p.get("offerings", [])[:6]:
                out.append(f"  - {o}")
            for n in p.get("what_you_need", [])[:2]:
                out.append(f"  - ✅ {n}")
            d = p.get("details", {})
            for k, label in (("hours", "🕗"), ("prices", "💷"),
                             ("location_in_building", "📍"), ("contact", "☎️")):
                if d.get(k):
                    out.append(f"  - {label} {d[k][:140]}")
            if p.get("notes"):
                out.append(f"  - ⚠️ _{p['notes']}_")
        for c in r.get("capabilities", [])[:3]:
            if isinstance(c, dict):
                out.append(f"\n💎 **{c.get('capability', '')}** — "
                           f"{c.get('detail', '')[:160]}")
        if not r.get("pages") and not r.get("capabilities"):
            out.append("\nI don't have a Hive page on that — try the council "
                       f"pages, or browse [thehiveworcester.org]({r['page_url']}).")
    as_of = (r.get("as_of") or "")[:10]
    out.append(f"\n<sub>ℹ️ From the Hive's own pages (crawled {as_of}). For live "
               "events, membership and prices the council site is the "
               "authority — ask me and I'll check it live.</sub>")
    out.append(f"🔎 [thehiveworcester.org]({r['page_url']})")
    return "\n".join(out)


def render_account_and_loans(r):
    focus = r.get("focus", "general")
    out = []

    if focus == "computer" and r.get("computer_booking"):
        cb = r["computer_booking"]
        out = ["💻 **Public computers at Worcestershire libraries:**\n", cb["summary"],
               f"\n✅ **What you need:** {cb['what_you_need']}"]
        for step in cb.get("how_to", []):
            out.append(f"- {step}")
        out.append(f"\n🔎 [Book a computer session]({cb['url']})")
        return "\n".join(out)

    if focus == "volunteer" and r.get("volunteering"):
        vol = r["volunteering"]
        out = ["🙋 **Volunteering at Worcestershire Libraries:**\n", vol["summary"],
               f"\n✅ **How to get involved:** {vol['what_you_need']}"]
        if vol.get("also_see"):
            out.append(f"\n💡 {vol['also_see']}")
        out.append(f"\n🔎 [Discover volunteering opportunities]({vol['url']})")
        return "\n".join(out)

    if focus == "digital_skills" and r.get("digital_skills"):
        ds = r["digital_skills"]
        out = ["🖥️ **Free digital skills support at your library:**\n", ds["summary"],
               f"\n✅ **Entry:** {ds['what_you_need']}"]
        for svc in ds.get("services", []):
            out.append(f"- {svc}")
        out.append(f"\n🔎 [Digital Inclusion — Helping You Online]({ds['url']})")
        return "\n".join(out)

    if focus == "reading_well" and r.get("reading_well"):
        rw = r["reading_well"]
        out = ["📖 **Reading Well — free wellbeing books at your library:**\n",
               rw["summary"], f"\n✅ **What you need:** {rw['what_you_need']}"]
        for col in rw.get("collections", []):
            out.append(f"- {col}")
        out.append(f"\n🔎 [Reading Well collections]({rw['url']})")
        return "\n".join(out)

    if focus == "room_hire" and r.get("room_hire_data"):
        rh = r["room_hire_data"]
        out = ["🏢 **Meeting rooms for hire at Worcestershire libraries:**\n",
               rh["summary"], f"\n✅ **What you need:** {rh['what_you_need']}"]
        for step in rh.get("how_to", []):
            out.append(f"- {step}")
        if rh.get("also_see"):
            out.append(f"\n💡 {rh['also_see']}")
        out.append(f"\n🔎 [Hire a library meeting room]({rh['url']})")
        return "\n".join(out)

    if focus == "warm_space" and r.get("warm_space"):
        ws = r["warm_space"]
        out = ["☕ **Warm, welcoming spaces — free at every library:**\n", ws["summary"],
               f"\n✅ **Entry:** {ws['what_you_need']}"]
        if ws.get("also_see"):
            out.append(f"\n💡 {ws['also_see']}")
        out.append(f"\n🔎 [Warm Welcome]({ws['url']})")
        return "\n".join(out)

    if focus == "adult_learning" and r.get("adult_learning"):
        al = r["adult_learning"]
        out = ["📚 **Adult learning & skills — free at Worcestershire Libraries:**\n",
               al["summary"], f"\n✅ **What you need:** {al['what_you_need']}\n"]
        for i, step in enumerate(al["how_to"], 1):
            out.append(f"{i}. {step}")
        out.append(f"\n💡 _{al['also_see']}_")
        out.append(f"\n🔎 [Learn, Upskill and Find Work]({al['url']})")
        return "\n".join(out)

    if focus == "book_clubs_reading" and r.get("book_clubs_reading"):
        bc = r["book_clubs_reading"]
        out = ["📖 **Book clubs & reading groups:**\n", bc["summary"],
               f"\n✅ **What you need:** {bc['what_you_need']}\n"]
        for i, step in enumerate(bc["how_to"], 1):
            out.append(f"{i}. {step}")
        if bc.get("also_see"):
            out.append(f"\n💡 _{bc['also_see']}_")
        out.append(f"\n🔎 [Library events & activities]({bc['url']})")
        return "\n".join(out)

    if focus == "donations" and r.get("donations"):
        d = r["donations"]
        out = ["📦 **Donating books to the library:**\n", d["summary"],
               f"\n✅ **Please note:** {d['what_you_need']}\n"]
        for i, step in enumerate(d["how_to"], 1):
            out.append(f"{i}. {step}")
        out.append(f"\n🔎 [Worcestershire Libraries]({d['url']})")
        return "\n".join(out)

    if focus == "school_visits" and r.get("school_visits"):
        sv = r["school_visits"]
        out = ["🏫 **School & group visits to Worcestershire Libraries:**\n", sv["summary"],
               f"\n✅ **What to do:** {sv['what_you_need']}\n"]
        for i, step in enumerate(sv["how_to"], 1):
            out.append(f"{i}. {step}")
        if sv.get("also_see"):
            out.append(f"\n💡 _{sv['also_see']}_")
        out.append(f"\n🔎 [Children's library services]({sv['url']})")
        return "\n".join(out)

    if focus == "accessible_formats" and r.get("accessible_formats"):
        af = r["accessible_formats"]
        out = ["♿ **Accessible reading at Worcestershire Libraries:**\n", af["summary"],
               f"\n✅ **What you need:** {af['what_you_need']}\n"]
        for opt in af.get("options", []):
            out.append(f"- {opt}")
        if af.get("also_see"):
            out.append(f"\n💡 _{af['also_see']}_")
        if af.get("catalogue_tip"):
            out.append(f"\n🔍 _{af['catalogue_tip']}_")
        out.append(f"\n🔎 [Read & Discover]({af['url']})")
        return "\n".join(out)

    if focus == "update_details" and r.get("update_details"):
        ud = r["update_details"]
        out = ["✏️ **Update your library account details:**\n", ud["summary"], "\n"]
        for step in ud.get("how_to", []):
            out.append(f"- {step}")
        if ud.get("also_see"):
            out.append(f"\n💡 _{ud['also_see']}_")
        out.append(f"\n🔎 [Library account login]({ud['url']})")
        return "\n".join(out)

    if focus == "lost_card" and r.get("lost_card"):
        lc = r["lost_card"]
        out = ["🃏 **Lost or stolen library card:**\n", lc["summary"], "\n"]
        for i, step in enumerate(lc["what_to_do"], 1):
            out.append(f"{i}. {step}")
        if lc.get("also_see"):
            out.append(f"\n💡 _{lc['also_see']}_")
        out.append(f"\n🔎 [Fees and charges]({lc['url']})")
        return "\n".join(out)

    if focus == "card_expired" and r.get("card_expired"):
        ce = r["card_expired"]
        out = ["🪪 **Library card or membership expired:**\n", ce["summary"], "\n"]
        out.append(f"✅ **What you need:** {ce['what_you_need']}\n")
        for i, step in enumerate(ce["how_to"], 1):
            out.append(f"{i}. {step}")
        if ce.get("also_see"):
            out.append(f"\n💡 _{ce['also_see']}_")
        out.append(f"\n🔎 [Join or renew membership]({ce['url']})")
        return "\n".join(out)

    if focus == "wifi" and r.get("wifi_access"):
        wa = r["wifi_access"]
        out = ["📶 **Free Wi-Fi at Worcestershire Libraries:**\n", wa["summary"], "\n"]
        out.append(f"✅ **What you need:** {wa['what_you_need']}\n")
        out.append("**How to connect:**")
        for step in wa["how_to"]:
            out.append(f"- {step}")
        if wa.get("also_see"):
            out.append(f"\n💡 _{wa['also_see']}_")
        out.append(f"\n🔎 [Worcestershire Libraries]({wa['url']})")
        return "\n".join(out)

    if focus == "self_service" and r.get("self_service"):
        ss = r["self_service"]
        out = ["🤖 **Self-service kiosks at Worcestershire Libraries:**\n", ss["summary"], "\n"]
        out.append(f"✅ **What you need:** {ss['what_you_need']}\n")
        out.append("**To borrow (check out):**")
        for step in ss["how_to_borrow"]:
            out.append(f"- {step}")
        out.append("\n**To return (check in):**")
        for step in ss["how_to_return"]:
            out.append(f"- {step}")
        if ss.get("also_see"):
            out.append(f"\n💡 _{ss['also_see']}_")
        out.append(f"\n🔎 [Your library account]({ss['url']})")
        return "\n".join(out)

    if focus == "ill" and r.get("ill_service"):
        ill = r["ill_service"]
        out = ["📦 **Inter-library loans — getting a book from another library service:**\n",
               ill["summary"], f"\n✅ **What you need:** {ill['what_you_need']}\n"]
        for i, step in enumerate(ill["how_to"], 1):
            out.append(f"{i}. {step}")
        if ill.get("also_see"):
            out.append(f"\n💡 _{ill['also_see']}_")
        out.append(f"\n🔎 [Worcestershire Libraries]({ill['url']})")
        return "\n".join(out)

    if focus == "loan_limits":
        ll, ren = r["loan_limits"], r["renewals"]
        out = ["📅 **Borrowing periods & limits:**\n", ll["summary"],
               f"\n- **Loan period:** {ll['loan_period']}",
               f"- **Digital (BorrowBox):** {ll['digital']}",
               f"- **Renewing:** {ren['summary']}",
               f"\n🔎 [Membership & borrowing]({ll['url']})"]
        return "\n".join(out)

    if focus == "pin_reset":
        pr = r["pin_reset"]
        out = ["🔑 **Forgotten your PIN?**\n", f"_{pr['summary']}_\n"]
        for step in pr["how_to"]:
            out.append(f"- {step}")
        out += [f"\n⚠️ _{pr['note']}_",
                f"\n🔎 [Login to your library account]({pr['url']})"]
        return "\n".join(out)

    if focus == "job_clubs" and r.get("job_clubs"):
        jc = r["job_clubs"]
        out = ["💼 **Library Job Clubs — free employment support:**\n", jc["summary"],
               f"\n✅ **What you need:** {jc['what_you_need']}\n"]
        for i, step in enumerate(jc["how_to"], 1):
            out.append(f"{i}. {step}")
        out.append(f"\n💡 _{jc['also_see']}_")
        out.append(f"\n🔎 [Job Clubs]({jc['url']})")
        return "\n".join(out)

    if focus == "home" and r.get("home_library"):
        h = r["home_library"]
        out += [
            "🏠 **Library Service at Home**\n",
            h["summary"],
            f"\n✅ **What you need:** {h['what_you_need']}",
            f"📞 **Contact:** {h['contact']}",
            f"\n{h['also_see']}",
            f"\n🔎 [Library Service at Home]({h['url']})",
        ]
        return "\n".join(out)

    if focus == "returning":
        ret = r["returning"]
        out += ["↩️ **Returning library items:**\n", f"_{ret['summary']}_\n"]
        for m in ret["methods"]:
            out.append(f"- {m}")
        out += [f"\n💡 _{ret['note']}_",
                f"\n🔎 [Your library account]({ret['url']})"]
        return "\n".join(out)

    if focus == "lost_item":
        li = r["lost_item"]
        out += ["⚠️ **Lost or damaged library item:**\n", f"_{li['summary']}_\n"]
        for step in li["what_to_do"]:
            out.append(f"- {step}")
        out.append(f"\n🔎 [Fees and charges]({li['url']})")
        return "\n".join(out)

    if focus == "ask_book" and r.get("ask_book"):
        ab = r["ask_book"]
        out += ["📖 **Ask for a Book — free personalised recommendations:**\n",
                ab["summary"], f"\n✅ **What you need:** {ab['what_you_need']}\n"]
        for i, step in enumerate(ab["how_to"], 1):
            out.append(f"{i}. {step}")
        out.append(f"\n🔎 [Ask for a Book]({ab['url']})")
        return "\n".join(out)

    if focus == "renewals":
        ren = r["renewals"]
        out += ["🔄 **Renewing your loans:**\n", f"_{ren['summary']}_\n"]
        for m in ren["methods"]:
            out.append(f"- {m}")
        out += [f"\n⚠️ _{ren['note']}_",
                f"\n🔎 [Renew a loan online]({ren['url']})"]

    elif focus == "fines":
        f_data = r["fines"]
        out += ["💷 **Fees and charges:**\n", f_data["summary"],
                f"\n- {f_data['how_to_pay']}",
                f"- {f_data['card_replacement']}",
                f"\n🔎 [Fees and charges]({f_data['url']})"]

    elif focus == "reservations":
        res = r["reservations"]
        out += ["🔖 **Reserving items:**\n",
                f"_{res['summary']}_ **{res['cost']}**\n"]
        for i, step in enumerate(res["how_to"], 1):
            out.append(f"{i}. {step}")
        if res.get("how_to_cancel"):
            out.append(f"\n↩️ **To cancel a reservation:** {res['how_to_cancel']}")
        out.append(f"\n🔎 [Reserve library books]({res['url']})")

    elif focus == "account":
        acc = r["account"]
        out += ["🔑 **Your library account:**\n", acc["summary"],
                f"\n✅ **What you need:** {acc['what_you_need']}"]
        if acc.get("pin_reset"):
            out.append(f"\n🔐 **Forgotten your PIN?** {acc['pin_reset']}")
        out.append(f"\n🔎 {acc['how_to']}")

    else:  # general
        acc, ren = r["account"], r["renewals"]
        f_data, res = r["fines"], r["reservations"]
        out += [
            "🪪 **Your library account — manage it online:**\n",
            f"🔑 **[Sign in to your account]({acc['url']})** — {acc['summary']} "
            f"_{acc['what_you_need']}_",
            f"🔄 **[Renew loans]({ren['url']})** — {ren['summary']}",
            f"🔖 **[Reserve items]({res['url']})** — {res['summary']} {res['cost']}",
            f"💷 **[Fees & charges]({f_data['url']})** — {f_data['summary']}",
            f"\n🔎 [Your library membership]({r['page_url']})",
        ]

    if r.get("home_library") and focus != "home":
        h = r["home_library"]
        out.append(f"\n🏠 _Can't get to a library?_ **[Library Service at Home]"
                   f"({h['url']})** — volunteer-run home delivery of books.")

    return "\n".join(out)


def render_children(r):
    out = ["👧 **Children's library services in Worcestershire**\n", r["summary"],
           f"\n✅ **What you need:** {r['what_you_need']}\n", "**What's available:**"]
    for item in r["highlights"]:
        out.append(f"- {item}")
    out += [f"\n🔎 [Events & activities — what's on near you]({r['events_url']})",
            f"📚 [Children's eBooks & audiobooks on BorrowBox]({r['borrowbox_url']})",
            f"🪪 [Join the library free]({r['join_url']})"]
    return "\n".join(out)


RENDER = {
    "search_catalogue": render_catalogue, "whats_new": render_whats_new,
    "where_to_get": render_where_to_get, "hive_info": render_hive,
    "find_library": render_find_library, "mobile_library": render_mobile,
    "library_events": render_events, "online_hub": render_online_hub,
    "libraries_unlocked": render_unlocked, "membership_help": render_membership,
    "printing_help": render_printing, "graph_search": render_graph,
    "account_and_loans": render_account_and_loans,
    "children_services": render_children,
}


# --------------------------------------------------------------------------- #
# Behaviour-change layer — £-saved value receipt + EAST nudges + chips
# --------------------------------------------------------------------------- #

def value_receipt(tool, raw):
    if tool in ("search_catalogue", "whats_new") and raw.get("items"):
        return "💷 _Borrowing instead of buying ≈ **£9–£20 saved** per title._"
    if tool == "where_to_get" and raw.get("found"):
        return "💷 _Getting it from the library instead of buying ≈ **£9–£20 saved**._"
    if tool == "online_hub":
        items = raw.get("items", [])
        if items:
            n = items[0].get("name", "").lower()
            if "pressreader" in n:
                return "💷 _PressReader is typically **~£9.99/month** — free with your library card._"
            if "theory test" in n:
                return "💷 _Theory Test Pro costs **£29.99** to buy — free with your library card._"
            if "which" in n:
                return "💷 _Which? subscription is **£10.99/month** — free with your library card._"
            if "borrowbox" in n or "ebook" in n or "audiobook" in n:
                return "💷 _eBooks and audiobooks via BorrowBox are free — each would cost £9–£15 to buy._"
        return ("💷 _Free with your card — a newspaper or eBook subscription is "
                "**~£8–£12/month** you don't pay._")
    if tool == "printing_help":
        return "💷 _Far cheaper than a high-street print shop._"
    return ""


# (EAST: Easy=chips, Attractive=value, Social/Timely=nudge)
NUDGES = {
    "search_catalogue": ("💡 No time to visit? Many titles are free on **BorrowBox** tonight.",
                         ["Is {title} on BorrowBox?", "Reserve & collect — how?", "Hot takes on new books"]),
    "whats_new": ("💡 Reserve it free and collect at your branch.",
                  ["More like this", "Is {title} an eBook?", "What's on this week?"]),
    "find_library": (f"💡 Want in before/after staffed hours? "
                     f"[**Libraries Unlocked**]({ls.UNLOCKED_URL}) = 8am–8pm.",
                     ["Tell me about Libraries Unlocked", "What's on at {place}?", "How do I join?"]),
    "mobile_library": ("💡 Housebound? The **Home Library Service** brings books to your door.",
                       ["Home Library Service", "How do I join?", "Find my nearest library"]),
    "library_events": ("💡 Most events are free — just turn up.",
                       ["Children's events", "Do I need to book?", "Find my nearest library"]),
    "online_hub": ("💡 It's free with your card — set up tonight from your sofa.",
                   ["How do I sign up?", "What newspapers are there?", "BorrowBox limits"]),
    "libraries_unlocked": ("💡 It's free — just a quick one-off induction.",
                           ["How do I join the library?", "Is Malvern library open now?",
                            "What's on this week?"]),
    "printing_help": ("💡 No printer at home? Print from your phone, collect within 24h.",
                      ["Printing prices", "Find my nearest library", "How do I join?"]),
    "membership_help": ("💡 Digital membership is instant — no card needed.",
                        ["Set up digital membership", "What's the difference?", "What can I borrow online?"]),
    "graph_search": ("💡 Tell me what matters (late, café, study space) and I'll match a branch.",
                     ["Late-opening + café", "Find a book", "What's on this week?"]),
    "where_to_get": ("💡 Reservations are free — collect at any branch, or the van.",
                     ["Is it on BorrowBox?", "Which branch is nearest?", "How do I join?"]),
    "hive_info": ("💡 The Hive is open 8:30am–10pm every single day — and anyone can walk in.",
                  ["The Hive's archives", "Hire a room at the Hive", "Is the Hive open now?"]),
    "account_and_loans": (
        "💡 Renewing is quickest online — no queue, no trip to the library.",
        ["How do I renew my books?", "How do I make a reservation?", "Can you suggest a book for me?"]),
    "children_services": (
        "💡 Most children's sessions are free and need no booking — just turn up.",
        ["What's on this week?", "Children's eBooks", "Find my nearest library"]),
}
HELP_CHIPS = ["How do I get Wolf Hall?", "What's at The Hive?",
              "What's on this week?"]


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #

SYNTH_SYSTEM = (
    "You are the Worcestershire Libraries assistant. Answer ONLY from the LIVE "
    "DATA provided — never invent titles, times, prices, stops or facilities. Warm, "
    "concise, British English. Keep the markdown links and the ✅/🔎 lines from the "
    "data. Lead with what the person CAN do: if a service meets their need in a "
    "different way (e.g. they can't print AT home, but can send a job from home "
    "and collect it at any branch), open with that — never with a bare 'no'. "
    "Keep any provenance footnote (e.g. 'From the Hive's own pages') intact. "
    "If the data doesn't answer it, say so and point to the source link.")


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
    "- 📚 **Get a specific book** — which branch has it on the shelf *right now*, "
    "borrow the eBook tonight, or request it from another library service\n"
    "- 🔄 **Account & loans** — renew books online, reserve or cancel items, "
    "return items, pay fines, lost or stolen card, lost/damaged item\n"
    "- 🃏 **Lost, stolen or expired library card** — how to report it, get a replacement, renew membership\n"
    "- 📶 **Library Wi-Fi** — free, no password needed, how to connect\n"
    "- 🤖 **Self-service kiosks** — how to borrow and return items without staff\n"
    "- 📦 **Inter-library loans** — requesting items not held in Worcestershire\n"
    "- 📖 **Ask for a Book** — free personalised reading recommendations from a librarian\n"
    "- 📍 **Branch hours, 'open now?', facilities** — toilets, parking, study space\n"
    "- 🐝 **The Hive** (Worcester) — archives & archaeology, study spaces, room "
    "hire, open 8:30am–10pm daily\n"
    "- 🚐 **Mobile library** times for your village\n"
    "- 🏠 **Library Service at Home** — free home delivery for those who can't visit\n"
    "- 📅 **What's on** this week\n"
    "- 💻 **Free online** — newspapers (PressReader), eBooks (BorrowBox), family history (Ancestry), "
    "the OED, Oxford Reference, BFI films and more\n"
    "- 🖥️ **Computers & digital skills** — free public computers, Wi-Fi, and digital skills support\n"
    "- 🖨️ **Printing** from your phone (Print Your Way)\n"
    "- 🏢 **Room hire** — meeting rooms at libraries across the county\n"
    "- 📖 **Reading Well** — free curated books for mental health and wellbeing\n"
    "- 📖 **Book clubs & reading groups** — at various branches, free to join\n"
    "- 🙋 **Volunteering, job clubs & adult learning** — free employment support "
    "and skills courses\n"
    "- 👧 **Children's services** — Storytime, Rhymetime, the Summer Reading Challenge\n"
    "- 🏫 **School & group visits** — arrange a class visit or library tour\n"
    "- ♿ **Accessible formats** — large print, eAudiobooks, talking newspapers, "
    "Braille and specialist services\n"
    "- ☕ **Warm spaces** — free, no membership needed\n\n"
    "_Answers come from official sources — the council site and catalogue "
    "checked live, plus every page of the Hive's own site — and each answer "
    "names its source._")


# --------------------------------------------------------------------------- #
# Chat handler — yields (answer_text, chips_or_None)
# --------------------------------------------------------------------------- #

def respond(message, history):
    message = (message or "").strip()
    if not message:
        yield HELP, HELP_CHIPS
        return

    tr = Trace(message, MODEL_ID)
    tool, args, how, rms = route(message, history)
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
    # Chips carry concrete names, not pronouns, so the follow-up routes
    # correctly even in no-LLM mode. A chip whose slot we can't fill is dropped.
    slots = {}
    if raw.get("name"):
        slots["place"] = re.sub(r"\s+Library$", "", raw["name"])
    if raw.get("items"):
        t = (raw["items"][0].get("title") or "").strip()
        if t:
            slots["title"] = t if len(t) <= 30 else t[:29] + "…"

    def _fill(c):
        try:
            return c.format(**slots)
        except (KeyError, IndexError):
            return None
    chips = [c for c in map(_fill, chips) if c]
    checked = raw.get("checked", "")
    label = ("page-level KB" if tool == "hive_info" and raw.get("as_of")
             else "**live**")
    footer = (f"\n\n<sub>🔎 Checked {label}"
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
                "<p>Exactly where to get the book you want, everything The Hive "
                "offers, mobile library, events, printing and what's free online — "
                "from the council site, catalogue & every Hive page, with exactly "
                "what you need to sign up.</p></div>")

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
            ["How do I get Wolf Hall by Hilary Mantel?",
             "Can you suggest a book for me?",
             "How do I renew my library books?",
             "How do I reserve a book?",
             "How do I cancel a reservation?",
             "How do I return library books?",
             "Do you have Harry Potter audiobooks?",
             "What can I do at The Hive?",
             "Is Malvern library open now?",
             "Is there free Wi-Fi at the library?",
             "A late-opening library with a café and meeting rooms",
             "When does the mobile library visit Abberley?",
             "Can I read newspapers for free?",
             "Can I watch old British TV programmes for free?",
             "I can't get to the library — can books be delivered?",
             "Can I book a computer at the library?",
             "How do I access the Oxford English Dictionary?",
             "Are there books to help with mental health?",
             "How do I join the Summer Reading Challenge?",
             "Can I hire a meeting room at my local library?",
             "Can I volunteer at the library?",
             "I need help getting online — can the library help?",
             "I forgot my library PIN — what do I do?",
             "Are there adult learning courses at the library?",
             "Is there a book club at my local library?",
             "Can I donate books to the library?",
             "Do you have large print books?",
             "Can I arrange a school visit to the library?",
             "Do you have books for people with visual impairments?",
             "How do I change my address on my library account?",
             "Is there a Youth Hub at The Hive?",
             "I've lost my library card — what do I do?",
             "Is the library open on bank holidays?",
             "Are there any dementia-friendly activities at the library?",
             "Do you have activities for pre-school children?",
             "My library card has expired — how do I renew it?",
             "How do I connect to the library Wi-Fi?",
             "Can you get a book from another library service?",
             "How do I use the self-service machine to borrow books?"],
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

        def bot_turn(hist):
            if not hist or hist[-1]["role"] != "user":
                yield hist, *hide3()
                return
            msg = _content_text(hist[-1]["content"])
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
