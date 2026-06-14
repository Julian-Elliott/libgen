"""
library_sources.py — data-mining tools for Worcestershire Libraries,
at every granularity:

  service level   — every council library page         (library_kb.json, live)
  page level      — every page of thehiveworcester.org (hive_kb.json)
  item level      — the live SirsiDynix catalogue      (Atom feed)
  copy level      — per-branch holdings on an item's detail page, so we can
                    say WHERE to actually get the book/eBook someone wants

Sources & provenance policy:
  - the SirsiDynix Enterprise catalogue (wcc.ent.sirsidynix.net.uk) — live
  - worcestershire.gov.uk library pages — live + crawled KB (canonical)
  - thehiveworcester.org — crawled page-by-page (build_hive_kb.py); every
    fact carries its source page + crawl date. Where Hive and council pages
    conflict (hours, prices, membership), the COUNCIL page wins, and answers
    say which source they used. The Hive site's own events page is static,
    so live events always come from the council.

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
HIVE = "https://www.thehiveworcester.org"
BORROWBOX = "https://library.bolindadigital.com/worcestershire"
EXPLORE_PAST = "https://www.explorethepast.co.uk"
MOBILE_INDEX = f"{GOV}/council-services/libraries/your-library-membership/mobile-library"
EVENTS_URL = f"{GOV}/council-services/libraries/library-events-and-activities"
PRINTING_URL = f"{GOV}/council-services/libraries/printing-and-photocopying-services"
UNLOCKED_URL = f"{GOV}/council-services/libraries/libraries-unlocked"
JOIN_URL = f"{GOV}/council-services/libraries/your-library-membership/join-library"
ONLINE_HUB = f"{GOV}/council-services/libraries/online-library-hub"
ACCOUNT_URL = f"{GOV}/council-services/libraries/your-library-membership/login-my-library-account"
RENEW_URL = f"{GOV}/council-services/libraries/your-library-membership/renew-loan"
FEES_URL = f"{GOV}/council-services/libraries/your-library-membership/pay-fees-and-charges"
RESERVE_URL = f"{GOV}/council-services/libraries/your-library-membership/reserve-your-library-books"
HOME_LIBRARY_URL = f"{GOV}/council-services/libraries/your-library-membership/library-service-home"
MEMBERSHIP_HUB_URL = f"{GOV}/council-services/libraries/your-library-membership"
ASK_FOR_A_BOOK_URL = f"{GOV}/council-services/libraries/read-and-discover/ask-book"
ROOM_HIRE_URL = f"{GOV}/council-services/libraries/hire-library-meeting-room"
BOOK_COMPUTER_URL = f"{GOV}/council-services/libraries/your-library-membership/book-computer"
READ_DISCOVER_URL = f"{GOV}/council-services/libraries/read-and-discover"

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
        ea_url = ""
        if fields.get("Electronic Access"):
            m = re.search(r"https?://\S+", fields["Electronic Access"])
            ea_url = m.group(0).rstrip('".,)') if m else ""
        items.append({
            "title": title,
            "author": fields.get("author", ""),
            "format": fmt,
            "icon": _FORMAT_ICON.get(fmt, "📦"),
            "year": fields.get("Publication Date", "").split()[0] if fields.get("Publication Date") else "",
            "isbn": fields.get("ISBN", ""),
            "digital": digital,
            "electronic_access": ea_url,
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
    "hive_visit": "Nothing — The Hive is open to everyone, 8:30am–10pm every day.",
    "archives": "Free to visit Explore the Past (Level 2, The Hive); opening "
                f"times and document-ordering rules are on [Explore the Past]({EXPLORE_PAST}).",
    "ask_book": (f"Any adult — free, no prior membership needed to request. "
                 f"[Ask for a Book]({ASK_FOR_A_BOOK_URL})"),
    "computer": (f"Free library membership. [Book a computer session]({BOOK_COMPUTER_URL}) "
                 "online or just walk in — sessions available on demand or pre-booked."),
    "room_hire": f"Anyone can hire a library meeting room. [Check availability and book]({ROOM_HIRE_URL}).",
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


_WEEKDAY_IDX = {d.lower(): i for i, d in enumerate(DAY_NAMES)}


def _resolve_day(when: str | None, now: datetime):
    """Map a free-text day hint ('tomorrow', 'monday', 'tonight', …) to a
    (weekday_index, human_label, is_today) triple. Defaults to today."""
    today_idx = now.weekday()
    t = (when or "").lower()
    if "tomorrow" in t:
        idx = (today_idx + 1) % 7
        return idx, f"tomorrow ({DAY_NAMES[idx]})", False
    for name, idx in _WEEKDAY_IDX.items():
        if re.search(rf"\b{name}\b", t):
            label = f"today ({name.capitalize()})" if idx == today_idx else name.capitalize()
            return idx, label, idx == today_idx
    return today_idx, f"today ({DAY_NAMES[today_idx]})", True


def _day_status(staffed: str):
    """Open/closed summary for a day other than today (no 'now')."""
    if not staffed or "close" in staffed.lower():
        return "closed"
    return f"open {staffed}"


def find_library(name: str | None = None, when: str | None = None) -> dict:
    """Branch hours, address and facilities. `when` is a free-text day hint
    ('tomorrow', 'Monday', 'tonight'); absent or 'today'/'now' means today."""
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
    idx, day_label, is_today = _resolve_day(when, now)
    day_name = DAY_NAMES[idx]
    h = pick.get("hours", {}).get(day_name, {})
    staffed, unlocked = h.get("staffed", ""), h.get("unlocked", "")
    if is_today:
        open_now, status = _open_status(staffed, now.hour * 60 + now.minute)
    else:
        open_now, status = None, _day_status(staffed)
    return {
        "name": pick["name"], "address": pick.get("address", ""),
        "facilities": pick.get("facilities", []), "hours": pick.get("hours", {}),
        "libraries_unlocked": pick.get("libraries_unlocked", False),
        "day_label": day_label, "is_today": is_today,
        "staffed": staffed, "unlocked": unlocked,
        # legacy keys kept for backward-compatibility (now the *requested* day)
        "today": day_name, "today_staffed": staffed, "unlocked_today": unlocked,
        "open_now": open_now, "status": status,
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


BORROWING_POLICY = {
    "account": {
        "summary": ("Log in to renew loans, reserve items, cancel reservations, "
                    "update your details and change your PIN."),
        "what_you_need": "Your library card number + PIN.",
        "how_to": f"Visit [Login to my library account]({ACCOUNT_URL}).",
        "url": ACCOUNT_URL,
    },
    "renewals": {
        "summary": "Extend your loan by renewing — online, by phone, or in person.",
        "methods": [
            f"**Online (easiest):** [Sign in to your account]({ACCOUNT_URL}) "
            "with your card number + PIN, then choose 'Renew'.",
            "**In person:** Ask staff or use a self-service kiosk at any Worcestershire library.",
            "**By phone:** Call your local branch during staffed hours.",
        ],
        "note": ("You may not be able to renew if another member has a reservation on the item — "
                 "in that case, return it on time and the other member will be notified."),
        "url": RENEW_URL,
    },
    "fines": {
        "summary": ("Borrowing books is free. If items are not returned or renewed by the due "
                    "date, late fees will apply. A full list of fees and charges is available online."),
        "how_to_pay": (f"[Pay fees and charges]({FEES_URL}) online, or settle them in person "
                       "at any library."),
        "card_replacement": (f"Lost your library card? Visit any library and speak to staff — "
                             f"they can issue a replacement. Replacement charges are listed at "
                             f"[fees and charges]({FEES_URL})."),
        "url": FEES_URL,
    },
    "reservations": {
        "summary": ("Reserve items online — they'll be held at your chosen branch and "
                    "you'll be emailed when ready to collect."),
        "cost": "Reservations are free.",
        "how_to": [
            "Search for the item in the catalogue or browse online.",
            "Open the item page and sign in with your card number + PIN.",
            ("Choose 'Place Hold' and select your collection branch — "
             "any Worcestershire library or the mobile van."),
            "You'll get an email when it's ready to collect.",
        ],
        "url": RESERVE_URL,
    },
    "returning": {
        "summary": ("Return items at any Worcestershire library — even if you borrowed "
                    "from a different branch. Many branches have a 24-hour drop-box "
                    "outside for returns when the library is unstaffed."),
        "methods": [
            "Hand items to staff or use a **self-service kiosk** at any Worcestershire library.",
            "Post items in the **24-hour external drop-box** if your branch has one "
            "(available outside staffed hours, including Libraries Unlocked hours).",
            "Return to the **mobile library van** on its next visit.",
        ],
        "note": ("Items clear from your account automatically when checked in. "
                 "Returning to a different branch is fine — no need to go back to "
                 "the branch you borrowed from."),
        "url": ACCOUNT_URL,
    },
    "lost_item": {
        "summary": ("If you've lost or damaged a library item, let the library know "
                    "as soon as possible. A replacement charge applies to lost items; "
                    "damaged items may incur a smaller charge depending on severity."),
        "what_to_do": [
            "**Contact your library** — by phone during staffed hours, in person, "
            "or online via the council website.",
            f"**Lost item:** a replacement charge applies — see the [fees page]({FEES_URL}) "
            "for current rates.",
            "**Damaged item:** return it and speak to staff — minor wear may attract a "
            "small charge; severe damage may be treated as lost.",
        ],
        "url": FEES_URL,
    },
    "page_url": MEMBERSHIP_HUB_URL,
}

HOME_LIBRARY_SERVICE = {
    "summary": (
        "The Library Service at Home is a free, volunteer-run service for people who "
        "find it difficult or impossible to visit a library in person. Volunteers "
        "select and deliver books and other library materials directly to your home."
    ),
    "what_you_need": "Full library membership (free). Contact your local library to register.",
    "contact": "Call 01905 822722 or ask staff at any Worcestershire library.",
    "url": HOME_LIBRARY_URL,
    "also_see": (
        f"The **mobile library** also visits 160 villages across Worcestershire every "
        f"4–5 weeks — a great option for many rural residents. "
        f"[Mobile library timetables]({MOBILE_INDEX})"
    ),
}

ASK_FOR_A_BOOK = {
    "summary": (
        "Ask for a Book is a free personalised reading recommendation service. "
        "Tell the library what you enjoy and library staff will curate up to three "
        "books matched to your taste — ready to collect at your local branch."
    ),
    "what_you_need": (
        f"Any adult — free, no prior membership needed to make a request. "
        f"You'll need a library card to collect the books; [join free]({JOIN_URL}) "
        "online or at any branch."
    ),
    "how_to": [
        f"Visit the [Ask for a Book page]({ASK_FOR_A_BOOK_URL}) and fill in the short form.",
        "Describe what you enjoy — genres, favourite authors, themes, or things you "
        "want to avoid.",
        "A librarian will hand-pick up to three books matched to your preferences.",
        "You'll be notified when your recommendations are ready to collect at your "
        "chosen branch.",
    ],
    "url": ASK_FOR_A_BOOK_URL,
}

ROOM_HIRE = {
    "summary": (
        "Worcestershire libraries offer affordable meeting rooms and spaces for hire — "
        "suitable for business meetings, community groups, training sessions and events."
    ),
    "what_you_need": (
        "Anyone can hire a library meeting room — membership is not required. "
        "Spaces vary by branch; prices and availability are on the booking page."
    ),
    "how_to": [
        f"Browse available rooms and check pricing at [Hire a library meeting room]({ROOM_HIRE_URL}).",
        "Book online or contact the relevant library branch directly.",
    ],
    "also_see": (
        "The Hive (Worcester) also offers larger venues and specialist spaces — "
        "ask me about 'room hire at The Hive' for details."
    ),
    "url": ROOM_HIRE_URL,
}


def account_and_loans(query: str | None = None) -> dict:
    """
    Online account, renewing loans, fines / late fees, reservations, returning,
    lost/damaged items, personalised book recommendations, and Library Service at Home.
    Surfaces the right sub-topic from the query.
    """
    q = (query or "").lower()
    out = dict(BORROWING_POLICY)
    out["checked"] = _now()

    # More specific checks first to avoid substring false-positives.
    if any(w in q for w in ("renew", "renewal", "extend", "due date")):
        out["focus"] = "renewals"
    elif ("card" in q and any(w in q for w in ("lost", "replace", "replacement", "stolen"))):
        out["focus"] = "fines"  # card_replacement info lives in the fines section
    elif (any(w in q for w in ("lost", "damage", "damaged"))
          and any(w in q for w in ("book", "item", "dvd", "cd"))
          and "card" not in q):
        out["focus"] = "lost_item"
    elif any(w in q for w in ("return", "returning", "bring back", "drop off", "hand back")):
        out["focus"] = "returning"
    elif any(w in q for w in ("fine", "charge", "fee", "owe", "overdue", "late", "pay")):
        out["focus"] = "fines"
    elif any(w in q for w in ("reserve", "hold", "reservation", "request", "order")):
        out["focus"] = "reservations"
    elif any(w in q for w in ("account", "login", "log in", "sign in", "pin", "password")):
        out["focus"] = "account"
    elif any(w in q for w in ("home", "housebound", "deliver")):
        out["focus"] = "home"
    elif any(w in q for w in ("recommend", "ask for a book", "personalised",
                              "suggest a book", "suggestion", "what should i read",
                              "choose a book", "pick a book")):
        out["focus"] = "ask_book"
    else:
        out["focus"] = "general"

    if out["focus"] == "home" or any(
            w in q for w in ("home library", "housebound", "deliver")):
        out["home_library"] = HOME_LIBRARY_SERVICE

    if out["focus"] == "ask_book":
        out["ask_book"] = ASK_FOR_A_BOOK

    return out


_HUB_SYNONYMS = {
    "newspaper": "pressreader", "magazine": "pressreader", "news": "pressreader",
    "press": "pressreader", "guardian": "pressreader", "times newspaper": "pressreader",
    "family history": "ancestry", "ancestry": "ancestry", "genealogy": "ancestry",
    "ebook": "borrowbox", "audiobook": "borrowbox", "audio book": "borrowbox",
    "business": "cobra", "start a business": "cobra", "company": "cobra",
    "driving": "theory test pro", "theory test": "theory test pro",
    "driving test": "theory test pro", "dvsa": "theory test pro",
    "dictionary": "oxford english", "research": "ebsco", "journal": "ebsco",
    "patent": "espacenet", "patents": "espacenet", "intellectual property": "espacenet",
    "film": "bfi", "tv": "bfi", "television": "bfi", "movie": "bfi",
    "british film": "bfi", "archive film": "bfi", "old tv": "bfi",
    "biography": "national biography", "who was": "national biography",
    "reference": "oxford reference",
}


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


# --------------------------------------------------------------------------- #
# 6. Copy-level availability + the "where do I actually GET it" journey
# --------------------------------------------------------------------------- #

_STATUS_AVAILABLE = re.compile(r"\b(available|on shelf|in library)\b", re.I)
_STATUS_WORDS = re.compile(
    r"\b(available|on shelf|in library|checked out|on loan|due back|due \d|"
    r"in transit|on order|on hold(shelf)?|reserved|missing|lost|reference only|"
    r"not for loan|being catalogued)\b", re.I)
_CALL_NO = re.compile(r"\b([A-Z]{0,3}\s?\d{3}(?:\.\d+)?\s?[A-Z]{0,4}|[A-Z]{1,3}\s?FIC[A-Z ]*)\b")


def _branch_names() -> list[str]:
    names = [b["name"] for b in kb().get("branches", [])]
    extra = ["The Hive", "Mobile Library"]
    out = names + [n for n in extra if n not in names]
    # also match without the trailing " Library"
    return sorted(out, key=len, reverse=True)


def _item_availability(detail_url: str) -> dict:
    """
    Copy-level holdings for one catalogue item: which branch, what call number,
    what status — parsed from the SirsiDynix detail page.

    The page is JS-heavy, so we parse defensively (tables first, then a text
    scan anchored on known branch names) and FAIL SOFT: if we can't parse,
    we say so rather than guess. Returns
    {"copies":[{branch, call_number, status, available}], "parsed": bool}.
    """
    out = {"copies": [], "parsed": False, "detail_url": detail_url}
    if not detail_url:
        return out
    try:
        soup = BeautifulSoup(_get(detail_url).text, "html.parser")
    except Exception as e:
        out["error"] = str(e)
        return out

    branches = _branch_names()

    def add_copy(branch, call_no, status):
        status = re.sub(r"\s+", " ", status).strip()
        out["copies"].append({
            "branch": branch.strip(),
            "call_number": (call_no or "").strip(),
            "status": status,
            "available": bool(_STATUS_AVAILABLE.search(status)),
        })

    # Strategy 1 — a holdings table (header mentions library/branch + status)
    for table in soup.find_all("table"):
        head = " ".join(th.get_text(" ", strip=True).lower()
                        for th in table.find_all(["th"]))
        if not (("librar" in head or "branch" in head or "location" in head)
                and ("status" in head or "avail" in head or "call" in head)):
            continue
        headers = [th.get_text(" ", strip=True).lower()
                   for th in table.find_all("th")]
        def col(*words):
            return next((i for i, h in enumerate(headers)
                         if any(w in h for w in words)), None)
        c_lib, c_call, c_stat = (col("librar", "branch", "location"),
                                 col("call", "shelf"), col("status", "avail"))
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            lib = cells[c_lib] if c_lib is not None and c_lib < len(cells) else ""
            stat = cells[c_stat] if c_stat is not None and c_stat < len(cells) else ""
            call = cells[c_call] if c_call is not None and c_call < len(cells) else ""
            if lib and (stat or call):
                add_copy(lib, call, stat or "see catalogue")
        if out["copies"]:
            out["parsed"] = True
            return out

    # Strategy 2 — text scan anchored on known branch names
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    for b in branches:
        for m in re.finditer(re.escape(b), text, re.I):
            window = text[m.end():m.end() + 140]
            sm = _STATUS_WORDS.search(window)
            if sm:
                cm = _CALL_NO.search(window[:sm.start()])
                add_copy(b, cm.group(0) if cm else "", sm.group(0))
    # dedupe
    seen, copies = set(), []
    for c in out["copies"]:
        key = (c["branch"].lower(), c["status"].lower(), c["call_number"])
        if key not in seen:
            seen.add(key); copies.append(c)
    out["copies"] = copies
    out["parsed"] = bool(copies)
    return out


def _title_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def where_to_get(query: str) -> dict:
    """
    The full get-it journey for one title: every format the service holds and,
    for each, exactly where/how to get it TODAY —
        on the shelf at a named branch (copy-level, live)  →
        reserve it (sign in + Place Hold)                  →
        borrow tonight on BorrowBox (eBook / eAudiobook)   →
        not held: ask staff / check the catalogue yourself.
    """
    query = (query or "").strip()
    try:
        res = search_catalogue(query, limit=12)
    except Exception as e:  # catalogue unreachable — fail soft, keep the journey
        res = {"items": [], "error": f"catalogue unreachable: {e}"}
    base = {"query": query, "search_url": res.get("search_url",
            CATALOGUE_SEARCH + quote_plus(query)), "checked": _now()}
    if res.get("error") or not res.get("items"):
        base.update({"found": False, "routes": [{
            "route": "not_held",
            "advice": "Nothing matched in the catalogue. Staff can often get "
                      "titles from other library services — ask in any branch, "
                      "or try different search words.",
            "url": base["search_url"],
        }]})
        return base

    items = sorted(res["items"], key=lambda i: _title_sim(query, i["title"]),
                   reverse=True)
    best = items[0]
    cluster = [i for i in items if _title_sim(best["title"], i["title"]) > 0.6]
    routes = []

    # 1) digital first — instant, tonight, no waiting
    for fmt in ("eBook", "eAudiobook"):
        d = next((i for i in cluster if i["format"] == fmt), None)
        if d:
            routes.append({
                "route": "digital", "format": fmt, "title": d["title"],
                "author": d["author"],
                "url": d.get("electronic_access") or BORROWBOX,
                "direct_link": bool(d.get("electronic_access")),
                "need": ELIGIBILITY["borrow_digital"],
                "steps": ["Open the link (or the BorrowBox app, service "
                          "'Worcestershire').",
                          "Sign in with your library card number + PIN — or get "
                          "instant digital membership with just a postcode.",
                          "Borrow it free; it auto-returns, so no fines."],
            })

    # 2) physical — copy-level: which branch has it on the shelf right now
    phys = next((i for i in cluster if not i["digital"]), None)
    if phys:
        avail = _item_availability(phys["detail_url"])
        on_shelf = [c for c in avail["copies"] if c["available"]]
        if on_shelf:
            routes.append({
                "route": "shelf", "format": phys["format"],
                "title": phys["title"], "author": phys["author"],
                "copies": on_shelf[:8], "all_copies": avail["copies"][:12],
                "need": ELIGIBILITY["borrow_physical"],
                "url": phys["detail_url"],
            })
        else:
            routes.append({
                "route": "reserve", "format": phys["format"],
                "title": phys["title"], "author": phys["author"],
                "copies": avail["copies"][:8],
                "availability_parsed": avail["parsed"],
                "need": ELIGIBILITY["borrow_physical"],
                "url": phys["detail_url"],
                "steps": ["Open the item page and sign in (card number + PIN).",
                          "Choose 'Place Hold' and pick the branch you want to "
                          "collect from — any Worcestershire library or the van.",
                          "You'll be emailed when it's ready to collect. "
                          "Reservations are free."],
            })

    if not routes:
        routes.append({"route": "not_held",
                       "advice": "I found similar titles but not that exact one — "
                                 "check the matches below or ask staff in any branch.",
                       "url": base["search_url"]})

    other = [f'{i["icon"]} {i["title"]} ({i["format"]})'
             for i in items if i not in cluster][:4]
    base.update({
        "found": True, "best_title": best["title"], "author": best["author"],
        "formats_held": sorted({i["format"] for i in cluster}),
        "routes": routes, "other_matches": other,
    })
    return base


# --------------------------------------------------------------------------- #
# 7. The Hive — page-level KB of thehiveworcester.org (built by build_hive_kb.py)
# --------------------------------------------------------------------------- #

_HIVE_KB = None


def hive_kb() -> dict:
    global _HIVE_KB
    if _HIVE_KB is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "hive_kb.json")
        try:
            with open(path, encoding="utf-8") as f:
                _HIVE_KB = json.load(f)
        except FileNotFoundError:
            _HIVE_KB = {"pages": [], "hive_profile": {}, "generated": ""}
    return _HIVE_KB


# question word -> hive page slug fragments that answer it
_HIVE_INTENTS = {
    "hour": ["opening-hours"], "open": ["opening-hours"],
    "parking": ["getting-here"], "park": ["getting-here"],
    "get there": ["getting-here"], "directions": ["getting-here"],
    "bus": ["getting-here"], "train": ["getting-here"],
    "contact": ["contact-us"], "phone": ["contact-us"], "email": ["contact-us"],
    "archive": ["explore-the-past"], "archaeolog": ["explore-the-past"],
    "family history": ["explore-the-past"], "history": ["explore-the-past"],
    "study": ["book-a-space"], "room": ["space-for-hire", "book-a-space"],
    "hire": ["space-for-hire"], "meeting": ["space-for-hire"],
    "card": ["how-to-get-a-library-card"], "join": ["how-to-get-a-library-card"],
    "fine": ["fines-renewals"], "renew": ["fines-renewals"],
    "borrow": ["borrowing-reservations"], "reserve": ["borrowing-reservations"],
    "café": ["cafe"], "cafe": ["cafe"], "coffee": ["cafe"], "food": ["cafe"],
    "children": ["children"], "kids": ["children"],
    "business": ["business"], "student": ["student"], "young": ["youthhub"],
    "career": ["youthhub"], "collection": ["collections"],
}


def hive_info(topic: str | None = None) -> dict:
    """
    The Hive (Worcester's library) at PAGE granularity: the exact offering of
    each page of thehiveworcester.org, plus the profile of what makes it more
    than a branch (joint university+public library, Explore the Past archives
    & archaeology, 800+ study spaces, room hire, BIPC, Youth Hub...).
    """
    hk = hive_kb()
    prof = hk.get("hive_profile", {})
    pages = [p for p in hk.get("pages", []) if not p.get("error")]
    caps = prof.get("extended_capabilities", [])
    out = {
        "name": prof.get("name", "The Hive, Worcester"),
        "partnership": prof.get("partnership", ""),
        "address": prof.get("address", ""),
        "opening_hours": (prof.get("opening_hours") or {}).get("building", ""),
        "as_of": hk.get("generated", ""), "source_note": hk.get("note", ""),
        "page_url": HIVE, "checked": _now(),
    }

    if not (topic or "").strip():
        out.update({
            "kind": "overview",
            "capabilities": caps[:12],
            "page_count": len(pages),
            "sections": sorted({p.get("section", "") for p in pages}),
        })
        return out

    t = topic.lower()
    slugs: list[str] = []
    for word, frags in _HIVE_INTENTS.items():
        if word in t:
            slugs += frags
    scored = []
    for p in pages:
        score = 0.0
        if any(s in p["url"] for s in slugs):
            score += 6
        title = p.get("title", "").lower()
        terms = [w for w in re.findall(r"[a-z]{3,}", t)
                 if w not in ("the", "hive", "library", "what", "can", "you",
                              "about", "tell", "does", "have", "there")]
        for w in terms:
            if w in title:
                score += 3
            score += sum(0.5 for o in p.get("offerings", []) if w in o.lower())
            if w in p.get("summary", "").lower():
                score += 1
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])

    cap_hits = [c for c in caps
                if isinstance(c, dict) and any(
                    w in (c.get("capability", "") + c.get("detail", "")).lower()
                    for w in re.findall(r"[a-z]{4,}", t))][:4]

    out.update({
        "kind": "topic", "topic": topic,
        "pages": [{
            "title": p["title"], "url": p["url"], "summary": p.get("summary", ""),
            "offerings": p.get("offerings", [])[:10],
            "what_you_need": p.get("what_you_need", [])[:3],
            "details": p.get("details", {}),
            "notes": p.get("notes", ""),
        } for _, p in scored[:3]],
        "capabilities": cap_hits,
    })
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

    print("\n### 5. WHERE TO GET: 'wolf hall' (copy-level availability — LIVE) ###")
    res = where_to_get("wolf hall")
    print(f"  found={res['found']} best='{res.get('best_title')}' "
          f"formats={res.get('formats_held')}")
    for r in res.get("routes", []):
        if r["route"] == "shelf":
            spots = ", ".join(f"{c['branch']} ({c['call_number'] or 'ask staff'})"
                              for c in r["copies"][:4])
            print(f"  📚 ON SHELF now: {spots}")
        elif r["route"] == "reserve":
            print(f"  🔖 reserve ({len(r.get('copies', []))} copies tracked, "
                  f"parsed={r.get('availability_parsed')})")
        elif r["route"] == "digital":
            print(f"  💻 {r['format']} — direct link: {r['direct_link']}")
        else:
            print(f"  ✋ {r['route']}: {r.get('advice', '')[:70]}")

    print("\n### 6. HIVE INFO (page-level KB — offline) ###")
    res = hive_info()
    print(f"  {res['name']} — {res['opening_hours'][:60]}")
    print(f"  pages={res.get('page_count')} capabilities={len(res.get('capabilities', []))} "
          f"as_of={res.get('as_of', '')[:10]}")
    for q in ("archives and old documents", "hire a meeting room", "parking"):
        r = hive_info(q)
        tops = [p["title"] for p in r.get("pages", [])]
        print(f"  '{q}' -> pages {tops} + {len(r.get('capabilities', []))} capabilities")
