"""
library_sources.py — live data-mining tools for Worcestershire Libraries.

Every function here pulls *live* data from official sources only:
  - the SirsiDynix Enterprise catalogue  (wcc.ent.sirsidynix.net.uk)
  - worcestershire.gov.uk library pages

We deliberately do NOT scrape thehiveworcester.org — that content is
unreliable and often years out of date. Council + catalogue only.

No LLM, no Gradio in here — pure functions so they can be unit-tested
against the live sites on their own.
"""

from __future__ import annotations

import html
import json
import os
import re
import difflib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

GOV = "https://www.worcestershire.gov.uk"
CATALOGUE_RSS = "https://wcc.ent.sirsidynix.net.uk/client/rss/hitlist/wcc/qu="
CATALOGUE_SEARCH = (
    "https://wcc.ent.sirsidynix.net.uk/client/en_GB/wcc/search/results?qu="
)
MOBILE_INDEX = f"{GOV}/council-services/libraries/your-library-membership/mobile-library"
EVENTS_URL = f"{GOV}/council-services/libraries/library-events-and-activities"
PRINTING_URL = f"{GOV}/council-services/libraries/printing-and-photocopying-services"
UNLOCKED_URL = f"{GOV}/council-services/libraries/libraries-unlocked"
JOIN_URL = f"{GOV}/council-services/libraries/your-library-membership/join-library"
ONLINE_HUB = f"{GOV}/council-services/libraries/online-library-hub"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "WorcsLibrariesAssistant/2.0 (+hackathon demo)"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}
TIMEOUT = 15


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M on %d %b %Y UTC")


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- #
# 1. Catalogue search  (SirsiDynix Atom feed)
# --------------------------------------------------------------------------- #

ATOM = "{http://www.w3.org/2005/Atom}"

_FORMAT_ICON = {
    "Books": "📖",
    "Large print": "📖",
    "Sound recording": "🎧",
    "Music recording": "🎵",
    "Video disc": "📀",
    "DVD": "📀",
    "eBook": "💻",
    "eAudiobook": "🎧",
}


def _classify(fields: dict) -> tuple[str, bool]:
    """Return (friendly_format, is_digital)."""
    fmt = fields.get("Format", "").strip()
    ea = fields.get("Electronic Access", "")
    digital = bool(ea) or "e" == fmt[:1].lower() and "ebook" in fmt.lower()
    if "eAudiobook" in ea or "eAudiobook" in fmt:
        return "eAudiobook", True
    if "eBook" in ea or "eBook" in fmt or ea:
        return ("eBook", True) if "audio" not in ea.lower() else ("eAudiobook", True)
    return fmt or "Item", digital


def search_catalogue(query: str, limit: int = 8) -> dict:
    """
    Search the live Worcestershire library catalogue.

    Returns {"query", "count", "items": [...], "search_url", "checked"}.
    Each item: title, author, format, year, isbn, digital, detail_url.
    """
    query = (query or "").strip()
    if not query:
        return {"query": "", "count": 0, "items": [], "error": "empty query"}

    url = CATALOGUE_RSS + quote_plus(query)
    r = _get(url)
    items: list[dict] = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        return {"query": query, "count": 0, "items": [],
                "error": f"feed parse error: {e}",
                "search_url": CATALOGUE_SEARCH + quote_plus(query)}

    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip().rstrip(".")
        cat_id = (entry.findtext(f"{ATOM}id") or "").strip()
        detail = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("rel") == "alternate":
                detail = link.get("href", "")
                break

        content = entry.findtext(f"{ATOM}content") or ""
        content = content.replace("<br/>", "\n").replace("<br>", "\n")
        content = html.unescape(content).replace("\xa0", " ")

        fields: dict[str, str] = {}
        for seg in content.split("\n"):
            seg = seg.strip()
            if not seg:
                continue
            if seg.lower().startswith("by "):
                fields["author"] = seg[3:].strip().rstrip(".")
                continue
            for label in ("Format", "Publication Date", "ISBN", "Edition",
                          "Language", "Electronic Access", "Call Number", "Series"):
                if seg.startswith(label):
                    fields[label] = seg[len(label):].strip()
                    break

        fmt, digital = _classify(fields)
        items.append({
            "title": title,
            "author": fields.get("author", ""),
            "format": fmt,
            "icon": _FORMAT_ICON.get(fmt, "📦"),
            "year": fields.get("Publication Date", "").split()[0] if fields.get("Publication Date") else "",
            "isbn": fields.get("ISBN", ""),
            "digital": digital,
            "detail_url": detail,
            "catalogue_id": cat_id,
        })
        if len(items) >= limit:
            break

    return {
        "query": query,
        "count": len(items),
        "total_hint": len(root.findall(f"{ATOM}entry")),
        "items": items,
        "search_url": CATALOGUE_SEARCH + quote_plus(query),
        "checked": _now(),
    }


# --------------------------------------------------------------------------- #
# 2. Mobile library timetable
# --------------------------------------------------------------------------- #

_village_cache: dict[str, str] | None = None


def _village_index() -> dict[str, str]:
    """Map normalised village name -> absolute timetable URL (cached)."""
    global _village_cache
    if _village_cache is not None:
        return _village_cache
    r = _get(MOBILE_INDEX)
    soup = BeautifulSoup(r.text, "html.parser")
    out: dict[str, str] = {}
    for a in soup.select('a[href*="/mobile-library/"]'):
        href = a.get("href", "")
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug == "mobile-library":
            continue
        name = slug.replace("-", " ").strip().lower()
        out.setdefault(name, urljoin(GOV, href))
    _village_cache = out
    return out


def mobile_library(place: str) -> dict:
    """
    Find the mobile-library timetable for a village/stop.

    Returns {"village", "date_of_operation", "stops":[{time,location}],
             "email", "page_url", "checked"} or {"error", "suggestions"}.
    """
    place = (place or "").strip().lower()
    index = _village_index()
    if not place:
        return {"error": "no place given", "suggestions": sorted(index)[:12]}

    # exact -> substring -> fuzzy
    name = None
    if place in index:
        name = place
    else:
        subs = [v for v in index if place in v or v in place]
        if subs:
            name = sorted(subs, key=len)[0]
        else:
            close = difflib.get_close_matches(place, index.keys(), n=3, cutoff=0.6)
            if close:
                name = close[0]

    if not name:
        sugg = difflib.get_close_matches(place, index.keys(), n=6, cutoff=0.3)
        return {
            "error": f"No mobile-library stop found matching '{place}'.",
            "suggestions": sugg or sorted(index)[:10],
            "page_url": MOBILE_INDEX,
        }

    url = index[name]
    soup = BeautifulSoup(_get(url).text, "html.parser")
    main = soup.find("main") or soup
    lines = [l.strip() for l in main.get_text("\n").split("\n") if l.strip()]

    date_op = ""
    for i, l in enumerate(lines):
        if l.lower() == "date of operation" and i + 1 < len(lines):
            date_op = lines[i + 1]
            break

    stop_re = re.compile(r"^\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\s*to\s+.+-\s+.+", re.I)
    stops = []
    for l in lines:
        if stop_re.match(l):
            tpart, _, loc = l.partition(" - ")
            stops.append({"time": tpart.strip(), "location": loc.strip()})

    return {
        "village": name.title(),
        "date_of_operation": date_op,
        "stops": stops,
        "email": "mobilelibraries@worcestershire.gov.uk",
        "page_url": url,
        "checked": _now(),
    }


# --------------------------------------------------------------------------- #
# 3. Library events & activities
# --------------------------------------------------------------------------- #

def _clean_label(text: str) -> str:
    """'Time: 9:30am to 2:00pm' -> '9:30am to 2:00pm'."""
    return re.sub(r"^[A-Za-z ]+:\s*", "", text or "").strip()


def library_events(query: str | None = None, limit: int = 8) -> dict:
    """
    Scrape upcoming library events/activities. Optional keyword filter.

    Returns {"count", "events":[{name,when,time,location,url}], "page_url", "checked"}.
    """
    soup = BeautifulSoup(_get(EVENTS_URL).text, "html.parser")
    events: list[dict] = []
    seen = set()

    # Each event is a Drupal `views-row` that links to /events/<slug>.
    for row in soup.select("div.views-row"):
        anchor = row.find("a", href=re.compile(r"/events/[a-z0-9-]+"))
        if not anchor:
            continue
        href = urljoin(GOV, anchor.get("href", ""))
        if href in seen:
            continue
        seen.add(href)

        title_el = row.select_one(".views-field-title")
        name = (title_el.get_text(" ", strip=True) if title_el
                else anchor.get_text(" ", strip=True))

        time_el = row.select_one(".event-time")
        days_el = row.select_one(".event-days")
        loc_el = row.select_one(".views-field-field-location")
        date_blob = row.select_one(".views-field-field-date-value")
        next_date = ""
        if date_blob:
            m = re.search(r"Date:\s*(.+?)\s*(?:How often|Time:|$)",
                          date_blob.get_text(" ", strip=True))
            if m:
                next_date = m.group(1).strip()

        events.append({
            "name": name,
            "next_date": next_date,
            "when": _clean_label(days_el.get_text(" ", strip=True)) if days_el else "",
            "time": _clean_label(time_el.get_text(" ", strip=True)) if time_el else "",
            "location": _clean_label(loc_el.get_text(" ", strip=True)) if loc_el else "",
            "url": href,
        })

    if query:
        q = query.lower()
        filt = [e for e in events
                if q in e["name"].lower() or q in e["location"].lower()]
        if filt:
            events = filt

    return {
        "count": len(events[:limit]),
        "events": events[:limit],
        "page_url": EVENTS_URL,
        "checked": _now(),
    }


# --------------------------------------------------------------------------- #
# 4. Printing — "Print Your Way"
# --------------------------------------------------------------------------- #

# Sourced from the official printing page (verified June 2026). Stable content,
# so served directly with a live source link rather than re-scraped each call.
PRINT_YOUR_WAY = {
    "summary": (
        "Print Your Way lets full library members send a print job from their own "
        "phone, tablet or computer and collect it from any Worcestershire library "
        "printer within 24 hours — great for job applications, forms, tickets and "
        "returns labels."
    ),
    "device_requirements": "Android 12+, iOS 16+, macOS 12 (Monterey)+, or Windows 11.",
    "steps": [
        f"Be a full library member — free, [join online]({JOIN_URL}) or in any library.",
        "Top up your PaperCut print account at a self-service kiosk in any "
        "Worcestershire library (some kiosks are cash-only, so check first).",
        "One-time setup: download and follow the Print Your Way guide for your "
        f"device — Android, iOS, macOS and Windows guides are on the "
        f"[printing page]({PRINTING_URL}).",
        "Open your document, choose the mono or colour print queue, set your "
        "options and send — authenticate with your library number and PIN.",
        "Release the job at any public printer in any Worcestershire library "
        "within 24 hours.",
    ],
    "pricing": {
        "A4 black & white": "15p per side",
        "A4 colour": "50p per side",
        "A3 black & white": "25p per side",
        "A3 colour": "85p per side",
    },
    "page_url": PRINTING_URL,
}


def printing_help() -> dict:
    out = dict(PRINT_YOUR_WAY)
    out["checked"] = _now()
    return out


# --------------------------------------------------------------------------- #
# 5. Knowledge base — every library service page (built by build_kb.py)
# --------------------------------------------------------------------------- #

_KB = None

def kb() -> dict:
    global _KB
    if _KB is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "library_kb.json")
        try:
            with open(path, encoding="utf-8") as f:
                _KB = json.load(f)
        except FileNotFoundError:
            _KB = {"branches": [], "online_hub": [], "services": [],
                   "membership_tiers": []}
    return _KB


# "What you need to sign up" — the eligibility that varies per service.
ELIGIBILITY = {
    "borrow_physical": f"Free **full membership** ([join online]({JOIN_URL}) + collect, "
                       "or in any library).",
    "borrow_digital": f"Free **digital membership** — instant, just a Worcestershire "
                      f"postcode ([sign up]({JOIN_URL})).",
    "printing": "Full membership + top up a PaperCut account at a library kiosk.",
    "mobile": "Free full membership — you can join on the van.",
    "unlocked": "Full member, aged 15+, after a short one-off staff induction.",
    "events": "Most events are free — just turn up; a few need booking.",
    "visit": "Nothing at all — anyone can walk in for Wi-Fi, toilets and study space.",
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]


def _uk_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=1)  # BST approx


def _to_minutes(t: str):
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t, re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        h += 12
    return h * 60 + int(m.group(2) or 0)


def _open_status(staffed: str, now_min: int):
    if not staffed or "close" in staffed.lower():
        return False, "closed today"
    rng = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(?:to|-|–)\s*"
                    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))", staffed, re.I)
    if not rng:
        return None, staffed
    a, b = _to_minutes(rng.group(1)), _to_minutes(rng.group(2))
    if a is None or b is None:
        return None, staffed
    if a <= now_min < b:
        return True, f"open now until {rng.group(2)}"
    if now_min < a:
        return False, f"opens at {rng.group(1)}"
    return False, f"closed now (was open until {rng.group(2)})"


def find_library(name: str | None = None) -> dict:
    """Branch hours ('open now?'), address and facilities (toilets/parking…)."""
    branches = kb().get("branches", [])
    if not (name or "").strip():
        return {"branches": [{"name": b["name"], "address": b.get("address", ""),
                              "url": b["url"]} for b in branches], "checked": _now()}
    names = {b["name"].lower(): b for b in branches}
    q = name.strip().lower()
    pick = next((b for n, b in names.items()
                 if q in n or n.replace(" library", "").strip() in q), None)
    if not pick:
        close = difflib.get_close_matches(q, names.keys(), n=1, cutoff=0.4)
        pick = names[close[0]] if close else None
    if not pick:
        return {"error": f"No Worcestershire library found matching '{name}'.",
                "suggestions": [b["name"] for b in branches[:8]],
                "page_url": f"{GOV}/council-services/libraries/find-library"}
    now = _uk_now()
    today = DAY_NAMES[now.weekday()]
    h = pick.get("hours", {}).get(today, {})
    is_open, status = _open_status(h.get("staffed", ""), now.hour * 60 + now.minute)
    return {
        "name": pick["name"], "address": pick.get("address", ""),
        "facilities": pick.get("facilities", []), "hours": pick.get("hours", {}),
        "libraries_unlocked": pick.get("libraries_unlocked", False),
        "today": today, "today_staffed": h.get("staffed", ""),
        "unlocked_today": h.get("unlocked", ""),
        "open_now": is_open, "status": status,
        "page_url": pick["url"], "checked": _now(),
    }


UNLOCKED_BRANCHES = ["Bromsgrove", "Droitwich", "Evesham", "Kidderminster",
                     "Malvern", "Pershore", "Redditch", "Rubery", "St John's",
                     "Stourport", "Tenbury"]


def libraries_unlocked(branch: str | None = None) -> dict:
    tier = next((t for t in kb().get("membership_tiers", [])
                 if "Unlocked" in t.get("tier", "")), {})
    out = {"branches": UNLOCKED_BRANCHES,
           "hours": "8:00am to 8:00pm, Monday to Saturday",
           "what_you_need": tier.get("what_you_need", ELIGIBILITY["unlocked"]),
           "unlocks": tier.get("unlocks", ""),
           "page_url": tier.get("url", UNLOCKED_URL),
           "checked": _now()}
    if branch:
        b = branch.strip().lower()
        out["branch_match"] = next(
            (x for x in UNLOCKED_BRANCHES if x.lower() in b or b in x.lower()), None)
    return out


# Curated, customer-grade detail for the online-hub resources. The crawler
# gets summaries; this adds the precise "what you need + how to access + what's
# inside" that the council pages bury. (Verified June 2026.)
# NB: we surface ACCESS + public TITLE COVERAGE only — never the licensed
# article content itself (that would breach PressReader/publisher terms).
CURATED_HUB = {
    "pressreader": {
        "what_you_need": "Worcestershire library card number + PIN (free full membership, residents). No separate PressReader account needed.",
        "at_home": True,
        "access": [
            "Get the PressReader app — [iPhone/iPad](https://apps.apple.com/app/pressreader/id313904711) "
            "or [Android](https://play.google.com/store/apps/details?id=com.newspaperdirect.pressreader.android) — "
            "or read in your browser at [pressreader.com](https://www.pressreader.com).",
            "Tap 'Libraries & Groups' and search/select 'Worcestershire'.",
            "Sign in with your library card number + PIN.",
            "At home you get a 30-day pass (re-confirm monthly); on library Wi-Fi it's 7 days.",
        ],
        "inside": "7,000+ full-page newspapers & magazines, 60+ languages, 120+ countries.",
        "titles": ["The Guardian", "The Independent", "Newsweek", "Vogue", "GQ",
                   "Hello!", "BBC Top Gear", "Le Monde", "El País"],
        "extras": "Free account adds offline download, article translation and listen-to-article audio.",
    },
    "borrowbox": {
        "what_you_need": "Library card number + PIN; choose 'Worcestershire' in the app.",
        "at_home": True,
        "access": [
            "Download the BorrowBox app — [iPhone/iPad](https://apps.apple.com/app/borrowbox-library/id562843562) "
            "or [Android](https://play.google.com/store/apps/details?id=com.bolindadigital.BorrowBoxLibrary).",
            "Select 'Worcestershire' as your library service.",
            "Sign in with your card number + PIN.",
        ],
        "inside": "Free eBooks & eAudiobooks — latest titles, award winners, non-fiction, classics and children's, plus the curated 'Worcestershire Reads' picks.",
        "limits": "Borrow up to 4 eBooks + 4 eAudiobooks at once; auto-returns (no fines); read/listen offline in the app.",
    },
    "ancestry": {
        "what_you_need": "Library membership. Ancestry Library Edition is normally used in the library / on library Wi-Fi — check the hub page for current at-home access.",
        "at_home": False,
        "inside": "Billions of historical records — census, births/marriages/deaths, military, immigration and more, spanning the 1500s–2000s.",
    },
    "theory test pro": {
        "what_you_need": "Library card; works at home.",
        "at_home": True,
        "inside": "Practise the official DVSA driving theory test (car, motorcycle, LGV/PCV) including hazard-perception clips.",
    },
    "which": {
        "what_you_need": "Library membership (often used in-branch — check the page).",
        "inside": "Independent product reviews and Best Buy buying advice.",
    },
    "times digital archive": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "Every page of The Times newspaper, scanned back to 1785.",
    },
    "access to research": {
        "what_you_need": "Free walk-in use on a library computer (in-branch).",
        "at_home": False,
        "inside": "Millions of academic journal articles across many disciplines — for students and independent researchers.",
    },
    "bfi": {
        "what_you_need": "Use on library computers / library Wi-Fi (in-branch).",
        "at_home": False,
        "inside": "Thousands of archive British films and TV programmes from the BFI National Archive.",
    },
    "cobra": {
        "what_you_need": "Library membership; supported by the Business & IP Centre (often used in-branch).",
        "at_home": False,
        "inside": "Business start-up guides, market research and reference for new and growing businesses.",
    },
    "digital library membership": {
        "what_you_need": "Any Worcestershire resident — sign up online in minutes with your postcode. No card needed.",
        "at_home": True,
        "inside": "Instant free access to eBooks, eAudiobooks, eMagazines & eNewspapers and more.",
    },
    "ebsco": {
        "what_you_need": "Library card + PIN; available at home.",
        "at_home": True,
        "inside": "Academic and reference databases — magazines, journals and research articles.",
    },
    "espacenet": {
        "what_you_need": "Free for everyone; support via the Business & IP Centre.",
        "at_home": True,
        "inside": "Search 150+ million patent documents worldwide (European Patent Office).",
    },
    "online events": {
        "what_you_need": "Free — book a place via the events listing.",
        "at_home": True,
        "inside": "Live digital talks and workshops you can join from home.",
    },
    "national biography": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "60,000+ biographies of notable people from British history (Oxford DNB).",
    },
    "oxford english": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "The complete Oxford English Dictionary — meanings, history and pronunciation.",
    },
    "oxford reference": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "Thousands of dictionaries and reference works across every subject.",
    },
    "oxford research": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "In-depth peer-reviewed research encyclopaedias across many fields.",
    },
}


def _merge_curated(name: str) -> dict:
    n = name.lower()
    for key, data in CURATED_HUB.items():
        if key in n or n in key:
            return data
    return {}


_HUB_SYNONYMS = {"newspaper": "pressreader", "magazine": "pressreader",
                 "family history": "ancestry", "ancestry": "ancestry",
                 "genealogy": "ancestry", "ebook": "borrowbox",
                 "audiobook": "borrowbox", "business": "cobra",
                 "driving": "theory", "theory test": "theory",
                 "dictionary": "oxford", "research": "ebsco"}


def online_hub(topic: str | None = None) -> dict:
    """Free-from-home digital resources (BorrowBox, PressReader, Ancestry…)."""
    hub = kb().get("online_hub", [])
    items = hub
    if topic:
        t = topic.lower()
        hits = []  # synonym-mapped resources rank first
        for k, v in _HUB_SYNONYMS.items():
            if k in t:
                hits += [h for h in hub if v in h["name"].lower()]
        hits += [h for h in hub if t in h["name"].lower()
                 or t in h.get("summary", "").lower()]
        if hits:
            seen, dedup = set(), []
            for h in hits:
                if h["url"] not in seen:
                    seen.add(h["url"]); dedup.append(h)
            items = dedup
    out_items = []
    for h in items[:8]:
        cur = _merge_curated(h["name"])
        out_items.append({
            "name": h["name"],
            "summary": cur.get("inside") or h.get("summary", "")[:200],
            "what_you_need": cur.get("what_you_need")
            or " ".join((h.get("what_you_need") or [ELIGIBILITY["borrow_digital"]])[:1]),
            "access": cur.get("access", []),
            "at_home": cur.get("at_home"),
            "limits": cur.get("limits", ""),
            "titles": cur.get("titles", []),
            "extras": cur.get("extras", ""),
            "url": h["url"],
        })
    return {
        "count": len(out_items),
        "items": out_items,
        "tiers": kb().get("membership_tiers", []),
        "page_url": ONLINE_HUB, "checked": _now(),
    }


def membership_help(service: str | None = None) -> dict:
    """What you need to sign up — the cross-service membership matrix."""
    out = {"tiers": kb().get("membership_tiers", []),
           "page_url": JOIN_URL,
           "checked": _now()}
    if service:
        s = service.lower()
        if any(w in s for w in ("print", "photocopy")):
            out["need"] = "Full membership + a topped-up PaperCut account."
        elif any(w in s for w in ("ebook", "audiobook", "borrowbox", "online",
                                  "magazine", "newspaper", "ancestry", "digital")):
            out["need"] = "Free digital membership — instant with a postcode."
        elif any(w in s for w in ("unlock", "8pm", "evening", "after hours")):
            out["need"] = "Full membership, 15+, plus a one-off induction."
        else:
            out["need"] = "Free full membership."
    return out


def whats_new(genre: str | None = None, limit: int = 6) -> dict:
    """Newest catalogue titles for a genre/topic — fuel for a fun 'hot take'."""
    term = (genre or "fiction").strip()
    res = search_catalogue(term, limit=30)
    items = [i for i in res.get("items", []) if i.get("year", "").isdigit()]
    items.sort(key=lambda i: i["year"], reverse=True)
    items = items or res.get("items", [])
    return {"genre": genre or "fiction", "items": items[:limit],
            "search_url": res.get("search_url", ""), "checked": _now()}


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import json

    print("\n### 1. CATALOGUE: 'harry potter' ###")
    res = search_catalogue("harry potter", limit=4)
    print(f"count={res['count']} (feed had ~{res.get('total_hint')}) checked {res.get('checked')}")
    for it in res["items"]:
        print(f"  {it['icon']} {it['title']} — {it['author']} [{it['format']} {it['year']}]")

    print("\n### 2. MOBILE LIBRARY: 'abberley' ###")
    res = mobile_library("abberley")
    if "error" in res:
        print("  ", res["error"], "→", res.get("suggestions"))
    else:
        print(f"  {res['village']} — {res['date_of_operation']} ({len(res['stops'])} stops)")
        for s in res["stops"][:4]:
            print(f"    {s['time']} — {s['location']}")

    print("\n### 2b. MOBILE LIBRARY fuzzy: 'kemsey' (typo) ###")
    res = mobile_library("kemsey")
    print("  ", res.get("village") or res.get("error"), res.get("suggestions", ""))

    print("\n### 3. EVENTS (filter: 'knit') ###")
    res = library_events("knit")
    for e in res["events"]:
        print(f"  • {e['name']} — {e['when']} {e['time']} @ {e['location']}")

    print("\n### 3b. EVENTS (all) ###")
    res = library_events()
    print(f"  {res['count']} events found")

    print("\n### 4. PRINTING ###")
    res = printing_help()
    print("  ", res["summary"][:80], "...")
    print("  pricing:", res["pricing"])
