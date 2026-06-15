"""
library_sources.py - data-mining tools for Worcestershire Libraries,
at every granularity:

  service level - every council library page (library_kb.json, live)
  page level - every page of thehiveworcester.org (hive_kb.json)
  item level - the live SirsiDynix catalogue (Atom feed)
  copy level - per-branch holdings on an item's detail page, so we can
                    say WHERE to actually get the book/eBook someone wants

Sources & provenance policy:
  - the SirsiDynix Enterprise catalogue (wcc.ent.sirsidynix.net.uk) - live
  - worcestershire.gov.uk library pages - live + crawled KB (canonical)
  - thehiveworcester.org - crawled page-by-page (build_hive_kb.py); every
    fact carries its source page + crawl date. Where Hive and council pages
    conflict (hours, prices, membership), the COUNCIL page wins, and answers
    say which source they used. The Hive site's own events page is static,
    so live events always come from the council.

No LLM, no Gradio in here - pure functions so they can be unit-tested
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
LIBRARY_PHONE = "01905 822722" # bookings/enquiries incl. Unlocked inductions
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
DIGITAL_INCLUSION_URL = f"{GOV}/council-services/libraries/learn-upskill-and-find-work/digital-inclusion-helping-you-online"
VOLUNTEERING_URL = f"{GOV}/council-services/libraries/learn-upskill-and-find-work/volunteering-training-and-work-experience"
READING_WELL_URL = f"{GOV}/council-services/libraries/read-and-discover/reading-well"
WARM_WELCOME_URL = f"{GOV}/council-services/libraries/warm-welcome"
LEARN_UPSKILL_URL = f"{GOV}/council-services/libraries/learn-upskill-and-find-work"
JOB_CLUBS_URL = f"{LEARN_UPSKILL_URL}/job-clubs"
CHILDREN_URL = f"{GOV}/council-services/libraries/read-and-discover"
SUMMER_READING_URL = f"{GOV}/council-services/libraries/read-and-discover/summer-reading-challenge"
CLOSING_DATES_URL = f"{GOV}/council-services/libraries/2026-libraries-closing-dates"

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
# 1. Catalogue search (SirsiDynix Atom feed)
# --------------------------------------------------------------------------- #

ATOM = "{http://www.w3.org/2005/Atom}"

_FORMAT_ICON = {
    "Books": "",
    "Large print": "",
    "Sound recording": "",
    "Music recording": "",
    "Video disc": "",
    "DVD": "",
    "eBook": "",
    "eAudiobook": "",
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
            "icon": _FORMAT_ICON.get(fmt, ""),
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
        return {
            "no_village": True,
            "guidance": (
                "The mobile library timetable is organised by village stop, not by date. "
                "Tell me your village or nearby area and I'll show you when the van visits."
            ),
            "example_villages": sorted(index)[:8],
            "page_url": MOBILE_INDEX,
            "email": "mobilelibraries@worcestershire.gov.uk",
            "checked": _now(),
        }

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
# 4. Printing - "Print Your Way"
# --------------------------------------------------------------------------- #

# Sourced from the official printing page (verified June 2026). Stable content,
# so served directly with a live source link rather than re-scraped each call.
PRINT_YOUR_WAY = {
    "summary": (
        "Print Your Way lets full library members send a print job from their own "
        "phone, tablet or computer and collect it from any Worcestershire library "
        "printer within 24 hours - great for job applications, forms, tickets and "
        "returns labels."
    ),
    "device_requirements": "Android 12+, iOS 16+, macOS 12 (Monterey)+, or Windows 11.",
    "steps": [
        f"Be a full library member - free, [join online]({JOIN_URL}) or in any library.",
        "Top up your PaperCut print account at a self-service kiosk in any "
        "Worcestershire library (some kiosks are cash-only, so check first).",
        "One-time setup: download and follow the Print Your Way guide for your "
        f"device - Android, iOS, macOS and Windows guides are on the "
        f"[printing page]({PRINTING_URL}).",
        "Open your document, choose the mono or colour print queue, set your "
        "options and send - authenticate with your library number and PIN.",
        "Release the job at any public printer in any Worcestershire library "
        "within 24 hours.",
    ],
    "pricing": {
        "A4 black & white": "15p per side",
        "A4 colour": "50p per side",
        "A3 black & white": "25p per side",
        "A3 colour": "85p per side",
    },
    "in_library_photocopy": {
        "summary": (
            "Walk-up photocopying is available at most Worcestershire libraries - "
            "no library card or phone needed, just bring your document and pay at "
            "the counter."
        ),
        "how_to": [
            "Take your original document to any Worcestershire library branch.",
            "Ask a member of staff to make copies, or use the self-service copier "
            "where available.",
            "Pay at the counter - cash and card are usually accepted.",
        ],
        "pricing": {
            "A4 black & white": "15p per side",
            "A4 colour": "50p per side",
            "A3 black & white": "25p per side",
            "A3 colour": "85p per side",
        },
        "note": (
            "Not every branch has a self-service copier; some rely on staff-assisted "
            f"copying. Call ahead on **{LIBRARY_PHONE}** or check with your local "
            "branch to confirm availability."
        ),
    },
    "scanning": {
        "summary": (
            "Document scanning (to USB stick or email) is available at some "
            "Worcestershire library branches - availability varies, so it's worth "
            "calling ahead before you travel."
        ),
        "how_to": [
            "Contact your local branch to confirm a scanner is available.",
            "Bring a USB stick for scan-to-USB, or be ready to provide your email "
            "address for scan-to-email.",
            "Ask staff about the charge - scanning is usually priced similarly to "
            "photocopying.",
        ],
        "note": (
            f"Call ahead on **{LIBRARY_PHONE}** to check scanner availability at "
            "your nearest branch - not all sites have this facility."
        ),
    },
    "page_url": PRINTING_URL,
}


def printing_help(query: str | None = None) -> dict:
    out = dict(PRINT_YOUR_WAY)
    out["checked"] = _now()
    q = (query or "").lower()
    if re.search(r"\bscan", q):
        out["focus"] = "scanning"
    elif re.search(r"\bphotocop", q):
        out["focus"] = "photocopy"
    else:
        out["focus"] = "print"
    return out


# --------------------------------------------------------------------------- #
# 5. Knowledge base - every library service page (built by build_kb.py)
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


# "What you need to sign up" - the eligibility that varies per service.
ELIGIBILITY = {
    "borrow_physical": f"Free **full membership** ([join online]({JOIN_URL}) + collect, "
                       "or in any library).",
    "borrow_digital": f"Free **digital membership** - instant, just a Worcestershire "
                      f"postcode ([sign up]({JOIN_URL})).",
    "printing": "Full membership + top up a PaperCut account at a library kiosk.",
    "mobile": "Free full membership - you can join on the van.",
    "unlocked": "Full member, aged 15+, after a short one-off staff induction.",
    "events": "Most events are free - just turn up; a few need booking.",
    "visit": "Nothing at all - anyone can walk in for Wi-Fi, toilets and study space.",
    "hive_visit": "Nothing - The Hive is open to everyone, 8:30am–10pm every day.",
    "archives": "Free to visit Explore the Past (Level 2, The Hive); opening "
                f"times and document-ordering rules are on [Explore the Past]({EXPLORE_PAST}).",
    "ask_book": (f"Any adult - free, no prior membership needed to request. "
                 f"[Ask for a Book]({ASK_FOR_A_BOOK_URL})"),
    "computer": (f"Free library membership. [Book a computer session]({BOOK_COMPUTER_URL}) "
                 "online or just walk in - sessions available on demand or pre-booked."),
    "room_hire": f"Anyone can hire a library meeting room. [Check availability and book]({ROOM_HIRE_URL}).",
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]


def _uk_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=1) # BST approx


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
           "get_started": [
               f"Pop into a Libraries Unlocked branch during **staffed hours** and "
               f"ask for the one-off induction - or call **{LIBRARY_PHONE}** to book "
               f"a slot in advance.",
               f"It takes a few minutes: staff show you how the self-service entry "
               f"and safety procedures work, then your card is upgraded - free.",
               f"Already a full member aged 15+? That's all you need. Not a member "
               f"yet? [Join online first]({JOIN_URL}) (free, takes minutes).",
           ],
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
# NB: we surface ACCESS + public TITLE COVERAGE only - never the licensed
# article content itself (that would breach PressReader/publisher terms).
CURATED_HUB = {
    "pressreader": {
        "what_you_need": "Worcestershire library card number + PIN (free full membership, residents). No separate PressReader account needed.",
        "at_home": True,
        "access": [
            "Get the PressReader app - [iPhone/iPad](https://apps.apple.com/app/pressreader/id313904711) "
            "or [Android](https://play.google.com/store/apps/details?id=com.newspaperdirect.pressreader.android) - "
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
            "Download the BorrowBox app - [iPhone/iPad](https://apps.apple.com/app/borrowbox-library/id562843562) "
            "or [Android](https://play.google.com/store/apps/details?id=com.bolindadigital.BorrowBoxLibrary).",
            "Select 'Worcestershire' as your library service.",
            "Sign in with your card number + PIN.",
        ],
        "inside": "Free eBooks & eAudiobooks - latest titles, award winners, non-fiction, classics and children's, plus the curated 'Worcestershire Reads' picks.",
        "limits": "Borrow up to 4 eBooks + 4 eAudiobooks at once; auto-returns (no fines); read/listen offline in the app.",
    },
    "ancestry": {
        "what_you_need": "Library membership. Ancestry Library Edition is normally used in the library / on library Wi-Fi - check the hub page for current at-home access.",
        "at_home": False,
        "inside": "Billions of historical records - census, births/marriages/deaths, military, immigration and more, spanning the 1500s–2000s.",
    },
    "theory test pro": {
        "what_you_need": "Library card; works at home.",
        "at_home": True,
        "inside": "Practise the official DVSA driving theory test (car, motorcycle, LGV/PCV) including hazard-perception clips.",
    },
    "which": {
        "what_you_need": "Library membership; available on library computers in-branch - ask staff for access.",
        "at_home": False,
        "inside": "Independent product reviews, Best Buy recommendations and consumer advice from Which? - covering technology, appliances, food, finance and more.",
        "access": [
            "Ask a member of staff at any Worcestershire library to access Which? "
            "on a library computer during staffed hours.",
            f"Or visit the [Online Library Hub]({ONLINE_HUB}) for the current Which? access link.",
        ],
    },
    "times digital archive": {
        "what_you_need": "Free digital membership.",
        "at_home": True,
        "inside": "Every page of The Times newspaper, scanned back to 1785.",
    },
    "access to research": {
        "what_you_need": "Free walk-in use on a library computer (in-branch).",
        "at_home": False,
        "inside": "Millions of academic journal articles across many disciplines - for students and independent researchers.",
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
        "what_you_need": "Any Worcestershire resident - sign up online in minutes with your postcode. No card needed.",
        "at_home": True,
        "inside": "Instant free access to eBooks, eAudiobooks, eMagazines & eNewspapers and more.",
    },
    "ebsco": {
        "what_you_need": "Library card + PIN; available at home.",
        "at_home": True,
        "inside": "Academic and reference databases - magazines, journals and research articles.",
    },
    "espacenet": {
        "what_you_need": "Free for everyone; support via the Business & IP Centre.",
        "at_home": True,
        "inside": "Search 150+ million patent documents worldwide (European Patent Office).",
    },
    "online events": {
        "what_you_need": "Free - book a place via the events listing.",
        "at_home": True,
        "inside": "Live digital talks and workshops you can join from home.",
    },
    "national biography": {
        "what_you_need": "Free digital membership - any Worcestershire resident, instant sign-up with a postcode.",
        "at_home": True,
        "access": [
            f"Visit the council's [Oxford DNB hub page]({GOV}/council-services/libraries/online-library-hub/oxford-dictionary-national-biography) and click the access link.",
            "Sign in with your Worcestershire library card number + PIN.",
            f"Not a member yet? [Get free digital membership instantly]({JOIN_URL}) - just a Worcestershire postcode needed.",
        ],
        "inside": "60,000+ biographies of notable people from British history - statespeople, scientists, artists, writers and more.",
    },
    "oxford english": {
        "what_you_need": "Free digital membership - any Worcestershire resident, instant sign-up with a postcode.",
        "at_home": True,
        "access": [
            f"Visit the council's [Oxford English Dictionary hub page]({GOV}/council-services/libraries/online-library-hub/oxford-english-dictionary) and click the access link.",
            "Sign in with your Worcestershire library card number + PIN.",
            f"Not a member yet? [Get free digital membership instantly]({JOIN_URL}) - just a Worcestershire postcode needed.",
        ],
        "inside": "The complete Oxford English Dictionary (OED) - meanings, history and pronunciation for 600,000+ words, old and new.",
    },
    "oxford reference": {
        "what_you_need": "Free digital membership - any Worcestershire resident, instant sign-up with a postcode.",
        "at_home": True,
        "access": [
            f"Visit the council's [Oxford Reference hub page]({GOV}/council-services/libraries/online-library-hub/oxford-reference) and click the access link.",
            "Sign in with your Worcestershire library card number + PIN.",
            f"Not a member yet? [Get free digital membership instantly]({JOIN_URL}) - just a Worcestershire postcode needed.",
        ],
        "inside": "Thousands of dictionaries, fact books and reference works across every subject - science, history, law, art, literature and more.",
    },
    "oxford research": {
        "what_you_need": "Free digital membership - any Worcestershire resident, instant sign-up with a postcode.",
        "at_home": True,
        "access": [
            f"Visit the council's [Oxford Research Encyclopaedias hub page]({GOV}/council-services/libraries/online-library-hub/oxford-research-encyclopaedias) and click the access link.",
            "Sign in with your Worcestershire library card number + PIN.",
            f"Not a member yet? [Get free digital membership instantly]({JOIN_URL}) - just a Worcestershire postcode needed.",
        ],
        "inside": "In-depth, peer-reviewed research encyclopaedias across many fields - clear guides for study and independent research.",
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
        "pin_reset": (
            f"Forgotten your PIN? Use the 'Forgot PIN' link on the "
            f"[account sign-in page]({ACCOUNT_URL}), or visit any library in person "
            "with your library card."
        ),
        "how_to": f"Visit [Login to my library account]({ACCOUNT_URL}).",
        "url": ACCOUNT_URL,
    },
    "renewals": {
        "summary": "Extend your loan by renewing - online, by phone, or in person.",
        "methods": [
            f"**Online (easiest):** [Sign in to your account]({ACCOUNT_URL}) "
            "with your card number + PIN, then choose 'Renew'.",
            "**In person:** Ask staff or use a self-service kiosk at any Worcestershire library.",
            "**By phone:** Call your local branch during staffed hours.",
        ],
        "note": ("You may not be able to renew if another member has a reservation on the item - "
                 "in that case, return it on time and the other member will be notified."),
        "url": RENEW_URL,
    },
    "fines": {
        "summary": ("Borrowing books is free. If items are not returned or renewed by the due "
                    "date, late fees will apply. A full list of current fees and charges "
                    f"is at [fees and charges]({FEES_URL})."),
        "charges_breakdown": [
            "**Overdue items** - a daily late fee per item applies until the item is returned "
            f"or renewed. Current rates at [fees and charges]({FEES_URL}).",
            "**Lost items** - a replacement charge applies, based on the cost of replacing "
            "the item. Let staff know as soon as possible.",
            "**Damaged items** - minor wear is usually accepted; significant damage may "
            "attract a partial or full replacement charge.",
            f"**Replacement library card** - a charge applies for a lost/stolen card; "
            f"see [fees and charges]({FEES_URL}) for the current amount.",
        ],
        "how_to_pay": (f"[Pay fees and charges online]({FEES_URL}) via your library account, "
                       "or pay in person at any Worcestershire library branch."),
        "note": (
            "Outstanding fees will not prevent you from borrowing, but they must be cleared "
            "before your membership can be renewed. BorrowBox eBooks and eAudiobooks never "
            "incur late fees - they auto-return on the due date."
        ),
        "url": FEES_URL,
    },
    "reservations": {
        "summary": ("Reserve items online - they'll be held at your chosen branch and "
                    "you'll be emailed when ready to collect."),
        "cost": "Reservations are free.",
        "how_to": [
            "Search for the item in the catalogue or browse online.",
            "Open the item page and sign in with your card number + PIN.",
            ("Choose 'Place Hold' and select your collection branch - "
             "any Worcestershire library or the mobile van."),
            "You'll get an email (or SMS if registered) when it's ready to collect.",
        ],
        "hold_duration": (
            "Items are typically held for **7 days** after your notification - "
            "collect within that window. If you can't make it, cancel and re-reserve "
            "so the item stays available to you."
        ),
        "if_missed": (
            "If a held item isn't collected within the hold period, it goes back into "
            "general circulation. Simply re-reserve it - no penalty applies."
        ),
        "how_to_cancel": (
            f"[Sign in to your account]({ACCOUNT_URL}), go to 'Reservations', "
            "select the item and choose 'Cancel Hold'. Or ask staff at any branch "
            "during staffed hours."
        ),
        "url": RESERVE_URL,
    },
    "returning": {
        "summary": ("Return items at any Worcestershire library - even if you borrowed "
                    "from a different branch. Many branches have a 24-hour drop-box "
                    "outside for returns when the library is unstaffed."),
        "methods": [
            "Hand items to staff or use a **self-service kiosk** at any Worcestershire library.",
            "Post items in the **24-hour external drop-box** if your branch has one "
            "(available outside staffed hours, including Libraries Unlocked hours).",
            "Return to the **mobile library van** on its next visit.",
        ],
        "note": ("Items clear from your account automatically when checked in. "
                 "Returning to a different branch is fine - no need to go back to "
                 "the branch you borrowed from."),
        "url": ACCOUNT_URL,
    },
    "lost_item": {
        "summary": ("If you've lost or damaged a library item, let the library know "
                    "as soon as possible. A replacement charge applies to lost items; "
                    "damaged items may incur a smaller charge depending on severity."),
        "what_to_do": [
            "**Contact your library** - by phone during staffed hours, in person, "
            "or online via the council website.",
            f"**Lost item:** a replacement charge applies - see the [fees page]({FEES_URL}) "
            "for current rates.",
            "**Damaged item:** return it and speak to staff - minor wear may attract a "
            "small charge; severe damage may be treated as lost.",
        ],
        "url": FEES_URL,
    },
    "loan_limits": {
        "summary": (
            "Standard loans are for 3 weeks - books, DVDs, CDs and most other "
            "physical items. You can renew as many times as needed, provided no "
            "other member has reserved the item. For the current limit on how many "
            "items you can borrow at once, see the membership page."
        ),
        "loan_period": "3 weeks (books, DVDs, CDs and other physical items).",
        "renewals": (
            "Renew online, by phone or in person - as many times as you need, "
            "unless another member has placed a reservation on the item."
        ),
        "digital": (
            "eBooks and eAudiobooks via BorrowBox have a **21-day loan period** - "
            "they auto-return on the due date (no fines, no action needed). "
            f"Borrow up to 4 eBooks and 4 eAudiobooks simultaneously. "
            f"[Get BorrowBox]({BORROWBOX})"
        ),
        "url": MEMBERSHIP_HUB_URL,
    },
    "pin_reset": {
        "summary": "Forgotten your library PIN? Reset it online, by phone or in person.",
        "how_to": [
            f"**Online (quickest):** Go to [Login to my library account]({ACCOUNT_URL}) "
            "and use the 'Forgotten PIN' link - you'll need your library card number.",
            "**In person:** Visit any Worcestershire library with proof of identity - "
            "staff can issue a new PIN on the spot.",
            f"**By phone:** Call **{LIBRARY_PHONE}** during staffed hours.",
        ],
        "note": (
            "If you've also lost your library card, get a replacement card in person "
            f"first (a small charge may apply - see [fees page]({FEES_URL})), then "
            "reset your PIN."
        ),
        "url": ACCOUNT_URL,
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
        f"4–5 weeks - a great option for many rural residents. "
        f"[Mobile library timetables]({MOBILE_INDEX})"
    ),
}

ASK_FOR_A_BOOK = {
    "summary": (
        "Ask for a Book is a free personalised reading recommendation service. "
        "Tell the library what you enjoy and library staff will curate up to three "
        "books matched to your taste - ready to collect at your local branch."
    ),
    "what_you_need": (
        f"Any adult - free, no prior membership needed to make a request. "
        f"You'll need a library card to collect the books; [join free]({JOIN_URL}) "
        "online or at any branch."
    ),
    "how_to": [
        f"Visit the [Ask for a Book page]({ASK_FOR_A_BOOK_URL}) and fill in the short form.",
        "Describe what you enjoy - genres, favourite authors, themes, or things you "
        "want to avoid.",
        "A librarian will hand-pick up to three books matched to your preferences.",
        "You'll be notified when your recommendations are ready to collect at your "
        "chosen branch.",
    ],
    "url": ASK_FOR_A_BOOK_URL,
}

ROOM_HIRE = {
    "summary": (
        "Worcestershire libraries offer affordable meeting rooms and spaces for hire - "
        "suitable for business meetings, community groups, training sessions and events."
    ),
    "what_you_need": (
        "Anyone can hire a library meeting room - membership is not required. "
        "Spaces vary by branch; prices and availability are on the booking page."
    ),
    "how_to": [
        f"Browse available rooms and check pricing at [Hire a library meeting room]({ROOM_HIRE_URL}).",
        "Book online or contact the relevant library branch directly.",
    ],
    "also_see": (
        "The Hive (Worcester) also offers larger venues and specialist spaces - "
        "ask me about 'room hire at The Hive' for details."
    ),
    "url": ROOM_HIRE_URL,
}

COMPUTER_BOOKING = {
    "summary": (
        "Free public computers are available at most Worcestershire libraries. "
        "Sessions can be booked in advance or simply used on a walk-in basis - "
        "no appointment needed."
    ),
    "what_you_need": "Free library membership (any tier, including instant digital membership).",
    "how_to": [
        f"[Book a computer session online]({BOOK_COMPUTER_URL}) using your library card number + PIN, "
        "or just walk in during staffed hours - computers are available on demand.",
        "Sessions are free and typically up to 1 hour (extensions may be available if machines are free).",
        "Free Wi-Fi is available at every library if you prefer to use your own device - no login needed.",
    ],
    "url": BOOK_COMPUTER_URL,
}

DIGITAL_SKILLS = {
    "summary": (
        "Worcestershire Libraries offer free digital skills support for everyone - "
        "whether you're getting online for the first time or want to build confidence "
        "with smartphones, email, online services or the internet."
    ),
    "what_you_need": "No membership needed to drop in - just turn up during staffed hours.",
    "services": [
        f"**[Digital Inclusion support page]({DIGITAL_INCLUSION_URL})** - links to all resources and guidance.",
        "**Learn My Way** - free, structured online courses to build digital skills at your own pace. "
        "Library staff can help you sign up and get started.",
        "**Digital Champions** - staff and volunteers available during staffed hours to help with "
        "devices, apps, online forms and internet safety.",
        "**Public computers and free Wi-Fi** - available at all libraries, no booking needed.",
        f"**[Get Online Week sessions]** - seasonal digital skills events; check [library events]({EVENTS_URL}) for dates.",
    ],
    "url": DIGITAL_INCLUSION_URL,
}

VOLUNTEERING = {
    "summary": (
        "Worcestershire Libraries welcomes volunteers in branches across the county. "
        "It's a rewarding way to give back to your community, learn new skills and meet people."
    ),
    "what_you_need": (
        f"No specific experience needed - just enthusiasm for libraries. Speak to staff "
        f"at your local library or call **{LIBRARY_PHONE}** to find out what opportunities "
        "are available near you."
    ),
    "also_see": (
        f"Young people aged 13–24 can volunteer during the **Summer Reading Challenge**. "
        f"Work experience and training placements are also available - see "
        f"[Volunteering, training and work experience]({VOLUNTEERING_URL})."
    ),
    "url": f"{GOV}/news/discover-volunteering-opportunities-your-local-library",
}

READING_WELL = {
    "summary": (
        "Reading Well is a free, curated book collection providing information and "
        "support for mental health, wellbeing and long-term health conditions. "
        "Endorsed by health professionals, all titles are available to borrow free "
        "from any Worcestershire library."
    ),
    "what_you_need": "Free library membership - borrow any Reading Well title from any branch.",
    "collections": [
        "**Reading Well for Adults** - books on managing mental health, anxiety, depression and long-term conditions.",
        "**Reading Well for Young People** - helping teenagers navigate difficult times.",
        "**Reading Well for Children** - supporting younger readers to understand their feelings.",
        "**Reading Well for Families** - guidance for families through pregnancy and early parenthood.",
    ],
    "url": READING_WELL_URL,
}

WARM_SPACE = {
    "summary": (
        "All Worcestershire libraries are free, warm and welcoming spaces - no "
        "membership needed and no need to buy anything. Part of the Warm Welcome "
        "Network, libraries offer a safe, comfortable environment with free Wi-Fi, "
        "computers, reading material and knowledgeable staff."
    ),
    "what_you_need": "Nothing - anyone can walk in during staffed hours.",
    "also_see": (
        "Library staff can also signpost visitors to local support services, "
        "community groups, health information and wellbeing resources."
    ),
    "url": WARM_WELCOME_URL,
}

JOB_CLUBS = {
    "summary": (
        "Library Job Clubs are free, friendly sessions where you can get help with "
        "CV writing, job applications, interview preparation and online job searching. "
        "Run at various Worcestershire libraries - no appointment usually needed."
    ),
    "what_you_need": "Free to attend - library membership is not required for Job Club sessions.",
    "how_to": [
        f"Check the [library events page]({EVENTS_URL}) for Job Club dates and locations near you.",
        "Drop in during the session, or call your local library to confirm times.",
        "Bring your CV or any job-related documents you'd like help with.",
    ],
    "also_see": (
        f"Also handy: free [public computers]({BOOK_COMPUTER_URL}) for job searching and CV "
        f"printing, and [adult learning courses]({LEARN_UPSKILL_URL}) to build new skills."
    ),
    "url": JOB_CLUBS_URL,
}

CHILDREN_SERVICES = {
    "summary": (
        "Worcestershire libraries run a wide range of free children's activities - "
        "Storytime, Rhymetime, Bounce and Rhyme, the Summer Reading Challenge, "
        "holiday events and more - across branches and at The Hive."
    ),
    "what_you_need": (
        "Most sessions are free and need no booking - just turn up. "
        "A few popular sessions may require advance booking; check the events listing."
    ),
    "highlights": [
        "**Storytime** - free weekly sessions at many branches for children aged 2–6; "
        "stories, songs and a craft activity.",
        "**Rhymetime / Bounce and Rhyme** - free sessions for babies and toddlers "
        "(typically 0–2 years), with nursery rhymes and movement songs. No booking needed at most branches.",
        "**Summer Reading Challenge** - the annual national reading challenge for "
        "children aged 4–11, free to join at any library during the summer holidays.",
        "**Holiday activities** - special events, crafts and clubs throughout school holidays.",
        "**Children's library at The Hive** - a dedicated children's floor with a wide "
        "selection of books for all ages.",
        f"**Children's eBooks & audiobooks** - available free via [BorrowBox]({BORROWBOX}).",
    ],
    "events_url": EVENTS_URL,
    "borrowbox_url": BORROWBOX,
    "join_url": JOIN_URL,
}

SUMMER_READING_CHALLENGE = {
    "summary": (
        "The Summer Reading Challenge is a free annual programme encouraging "
        "children to keep reading during the school summer holidays. "
        "Children aged 4–11 borrow and read any six library books between July "
        "and September, collecting stickers and rewards along the way - and "
        "earning a certificate for completing the challenge."
    ),
    "what_you_need": (
        "Free library membership for the child - sign up at any branch or online. "
        "Children register for the Challenge at the library from July each year."
    ),
    "how_to": [
        "Visit any Worcestershire library from July onwards to register your child "
        "- registration is free.",
        "Pick up your reading record booklet and choose your first books.",
        "Return to the library after every two books to collect stickers and rewards.",
        "Read six books by mid-September to earn a certificate (and a medal at "
        "many branches).",
    ],
    "also_see": (
        "Young people aged 13–24 can **volunteer** on the Summer Reading Challenge "
        "- great for CVs and the Duke of Edinburgh's Award. "
        f"See [volunteering and work experience]({VOLUNTEERING_URL}) for details."
    ),
    "url": SUMMER_READING_URL,
}

ADULT_LEARNING = {
    "summary": (
        "Worcestershire libraries support adult learning and skills development - "
        "including IT and computer courses, English and maths classes, and access "
        "to free online learning tools. Sessions are held in-branch and online."
    ),
    "what_you_need": (
        "Most courses and sessions are free to attend. Library membership may be "
        "needed to access online platforms like Learn My Way."
    ),
    "how_to": [
        f"Browse current courses and sessions on the [Learn, Upskill and Find Work]({LEARN_UPSKILL_URL}) page.",
        "Call your local library to ask what adult learning is available near you.",
        f"[Learn My Way]({DIGITAL_INCLUSION_URL}) is a free beginner-friendly platform "
        "covering internet basics, email, online safety and more - accessible from home.",
    ],
    "also_see": (
        f"[Job Clubs]({JOB_CLUBS_URL}) offer free CV and employment support; "
        f"[Digital Champions]({DIGITAL_INCLUSION_URL}) give one-to-one help getting online."
    ),
    "url": LEARN_UPSKILL_URL,
}

BOOK_CLUBS = {
    "summary": (
        "Worcestershire libraries host and support book clubs and reading groups - "
        "a free, friendly way to discover new books and share views with other readers. "
        "Groups meet regularly at various branches."
    ),
    "what_you_need": (
        "Usually free to attend. A library card is helpful so you can borrow reading "
        "copies. Contact your nearest library for details of local groups."
    ),
    "how_to": [
        f"Check the [library events listing]({EVENTS_URL}) for book club dates near you.",
        "Contact your nearest library branch to ask about active reading groups.",
        "Libraries can often supply reading copies of the chosen book for group members.",
    ],
    "also_see": (
        f"[PressReader]({ONLINE_HUB}) gives free access to thousands of literary "
        "magazines - great for keeping up with reviews between meetings."
    ),
    "url": EVENTS_URL,
}

TEEN_SERVICES = {
    "summary": (
        "Worcestershire libraries have plenty for teenagers and young adults - "
        "a Young Adult (YA) fiction section, free eBooks and audiobooks, "
        "volunteering opportunities and extended access for 15+."
    ),
    "highlights": [
        "**Young Adult (YA) fiction** - dedicated YA and teen sections at most branches, "
        "covering YA fiction, graphic novels, manga and non-fiction for teens.",
        f"**Free eBooks & audiobooks** - [BorrowBox]({BORROWBOX}) has a large YA catalogue "
        "of ebooks and audiobooks. Borrow up to 4 of each, auto-return, no fines - "
        "just a free library card.",
        f"**Libraries Unlocked** - members aged 15+ can access participating libraries "
        f"7 days a week, 8am–8pm, even when unstaffed. Requires a one-off induction at "
        f"your branch. [Find out more]({UNLOCKED_URL})",
        "**Summer Reading Challenge volunteering** - young people aged 13–24 can volunteer "
        f"in libraries during the Summer Reading Challenge (July–September). Great for CVs "
        f"and the Duke of Edinburgh's Award. [Volunteering details]({VOLUNTEERING_URL})",
        "**Teen events** - creative writing, gaming, craft and quiz events run throughout "
        f"the year at various branches. Check the [library events page]({EVENTS_URL}) for "
        "what's on near you.",
        f"**Youth Hub & Careers Hub at The Hive** (Worcester) - free careers advice, "
        "apprenticeship guidance and support for young people. Ask me about 'The Hive' "
        "for details.",
    ],
    "what_you_need": (
        "Free library membership for most services - join online instantly or in any branch. "
        f"Under-16s need a parent or guardian to sign up. [Join free]({JOIN_URL})"
    ),
    "url": READ_DISCOVER_URL,
    "events_url": EVENTS_URL,
    "borrowbox_url": BORROWBOX,
    "unlocked_url": UNLOCKED_URL,
}

DONATIONS = {
    "summary": (
        "Many Worcestershire library branches welcome donations of good-quality "
        "second-hand books. Donated books are typically sold at low cost in the library "
        "to raise funds for local library services."
    ),
    "what_you_need": (
        "Books should be clean, undamaged and in readable condition. "
        "Please contact your local branch before bringing a large donation, "
        "as acceptance and storage capacity varies by location."
    ),
    "how_to": [
        "Call or visit your nearest library to check they are accepting donations.",
        "Bring clean, undamaged books - no mould, heavy writing or water damage.",
        "Staff will assess donations and let you know how they will be used.",
    ],
    "url": f"{GOV}/council-services/libraries",
}

SCHOOL_VISITS = {
    "summary": (
        "Worcestershire libraries welcome school and group visits - tours, story "
        "sessions, reading challenge registration and library inductions for "
        "school classes. Visits help children build reading habits and discover "
        "everything the library offers."
    ),
    "what_you_need": (
        "Contact your local library branch in advance to arrange a group visit. "
        "Availability varies by branch and time of year - early booking is recommended."
    ),
    "how_to": [
        f"Call **{LIBRARY_PHONE}** or contact your local library directly to "
        "discuss a school or group booking.",
        "For visits to **The Hive** (Worcester), email "
        "**bookings@thehiveworcester.org** - the Hive has a dedicated Children's "
        "floor and a full programme of school activities.",
        "Describe what you'd like: a library tour, story session, Summer Reading "
        "Challenge sign-up, or a librarian-led reading lesson.",
        "Most visits are free - confirm availability and any requirements with "
        "the branch beforehand.",
    ],
    "also_see": (
        f"The **[Summer Reading Challenge]({SUMMER_READING_URL})** runs July–September "
        "and is free for all school-age children. "
        f"Young people aged 13–24 can **[volunteer]({VOLUNTEERING_URL})** on the "
        "challenge - great for CVs and the Duke of Edinburgh's Award."
    ),
    "url": f"{GOV}/council-services/libraries/read-and-discover",
    "hive_booking_email": "bookings@thehiveworcester.org",
}

ACCESSIBLE_FORMATS = {
    "summary": (
        "Worcestershire Libraries offer several ways to access reading material "
        "if you have a visual impairment, dyslexia or other print disability - "
        "including large print books, eAudiobooks via BorrowBox, talking newspapers "
        "via PressReader, and staff guidance on specialist services."
    ),
    "what_you_need": "Free library membership - most formats free to borrow.",
    "options": [
        "**Large print books** - available at all branches; search the catalogue "
        "with 'large print [title]' or ask staff to check availability. Free to borrow.",
        f"**eAudiobooks via BorrowBox** - borrow free with a library card; ideal "
        f"if print is difficult. Borrow up to 4 at once, auto-return, no fines. "
        f"[Get BorrowBox]({BORROWBOX})",
        f"**Talking newspapers & magazines via PressReader** - listen to The Guardian, "
        f"BBC Top Gear and 7,000+ titles with listen-to-article audio. "
        f"Typically £9.99/month - included with your library card. [PressReader]({ONLINE_HUB})",
        "**RNIB resources & Listening Books** - library staff can advise on how to "
        "access RNIB Talking Books, the Listening Books service, and other "
        "specialist accessible reading formats. Ask at any branch.",
        f"**Reading Well** - free curated books on mental health and long-term "
        f"conditions, available at all branches. "
        f"[Reading Well collections]({READING_WELL_URL})",
    ],
    "also_see": (
        "Ask staff at any branch for personal guidance on the format that suits "
        "you best. Many branches also carry DAISY format audiobooks and talking "
        "book collections - stock varies, so it's worth calling ahead."
    ),
    "url": READ_DISCOVER_URL,
    "catalogue_tip": (
        "Search the catalogue with 'large print [title]' or browse by format "
        "to find specific accessible editions."
    ),
}

UPDATE_DETAILS_INFO = {
    "summary": (
        "You can update your library account details - address, email, phone "
        "number - online, by phone or in person at any Worcestershire library."
    ),
    "how_to": [
        f"**Online (quickest):** Sign in to [your library account]({ACCOUNT_URL}) "
        "and go to 'My Account' or 'Personal Details' to update your information.",
        "**In person:** Visit any Worcestershire library - if changing your address, "
        "bring a recent utility bill or official letter as proof.",
        f"**By phone:** Call **{LIBRARY_PHONE}** during staffed hours.",
    ],
    "also_see": (
        "If you've forgotten your PIN, use 'Forgotten PIN' on the "
        f"[account sign-in page]({ACCOUNT_URL}), or ask staff at any branch."
    ),
    "url": ACCOUNT_URL,
}


LOST_CARD = {
    "summary": (
        "If you've lost your library card - or it's been stolen - let the library "
        "know as soon as possible so your account can be protected. You can get a "
        "replacement card at any Worcestershire library branch."
    ),
    "what_to_do": [
        f"**Contact the library now** - call **{LIBRARY_PHONE}** or visit any branch in "
        "person. Staff can flag the card as lost or stolen to prevent unauthorised use.",
        "**Get a replacement card in branch** - bring proof of identity (e.g. passport, "
        "driving licence, or a utility bill). A small replacement charge may apply - "
        f"see the [fees and charges page]({FEES_URL}) for current rates.",
        "**Change your PIN** - if you're concerned someone may have both your card and "
        f"PIN, reset it straight away via [your library account]({ACCOUNT_URL}) "
        f"(use 'Forgotten PIN'), or ask staff to reset it in branch.",
        "**Check your account online** - sign in to review your loans and reservations "
        f"and confirm no unexpected items have been borrowed. [Sign in]({ACCOUNT_URL})",
    ],
    "also_see": (
        "Your card number is also shown in any previous account correspondence. "
        "You can still log in and manage your account using your card number and PIN "
        "while you wait for a replacement card."
    ),
    "url": FEES_URL,
}


WIFI_ACCESS = {
    "summary": (
        "Free Wi-Fi is available at all Worcestershire libraries - no password, "
        "no login and no library card required. Just enable Wi-Fi on your device "
        "and join the library guest network."
    ),
    "what_you_need": "Nothing - Wi-Fi is open to all visitors during staffed hours.",
    "how_to": [
        "Enable Wi-Fi on your device and look for the library's guest network - "
        "the network name is usually posted on a sign in the branch.",
        "Tap to connect - no password or login screen required.",
        "Speed and time limits may apply; ask staff if you need longer or have "
        "trouble connecting.",
    ],
    "also_see": (
        f"Prefer a library computer? [Book a session online]({BOOK_COMPUTER_URL}) "
        "or simply walk in - free at most branches. "
        f"[Libraries Unlocked]({UNLOCKED_URL}) gives Wi-Fi access 8am–8pm Mon–Sat "
        "even when the branch is unstaffed."
    ),
    "url": f"{GOV}/council-services/libraries",
}

SELF_SERVICE = {
    "summary": (
        "Most Worcestershire libraries have self-service kiosks where you can "
        "borrow and return items without waiting for staff - quick and easy "
        "using your library card and PIN."
    ),
    "what_you_need": "Your library card (or card number) and PIN.",
    "how_to_borrow": [
        "At the kiosk, scan your library card barcode or type your card number.",
        "Enter your PIN when prompted.",
        "Place the item on the pad or scan its barcode to check it out.",
        "Collect your receipt - it shows the due date for each item.",
    ],
    "how_to_return": [
        "Scan your library card at the kiosk, then scan each item to return it - "
        "the screen confirms each successful check-in.",
        "Many branches also have a 24-hour external drop-box outside for returns "
        "when the library is closed or unstaffed.",
    ],
    "also_see": (
        f"[Renew loans online anytime]({RENEW_URL}) - no trip to the library needed. "
        f"[Reserve items]({RESERVE_URL}) for collection at any branch."
    ),
    "url": ACCOUNT_URL,
}

ILL_SERVICE = {
    "summary": (
        "If the item you want isn't in the Worcestershire library network, "
        "library staff can often request it from another library service - "
        "known as an inter-library loan. A small charge usually applies."
    ),
    "what_you_need": "Full library membership. Ask at any Worcestershire library branch.",
    "how_to": [
        f"First check the [Worcestershire catalogue]({CATALOGUE_SEARCH}) - including "
        "eBook and eAudiobook formats on BorrowBox, which may be available tonight.",
        "If the item isn't in the network, speak to staff at any branch or call "
        f"**{LIBRARY_PHONE}** - they'll advise whether an inter-library loan is possible.",
        "Provide the title, author, publisher and ideally the ISBN so staff can "
        "trace the item in another service's catalogue.",
        "A charge typically applies to cover processing and postage - staff will "
        "confirm the fee before placing the request.",
        "Items usually arrive within 2–4 weeks; you'll be notified when ready to collect.",
    ],
    "also_see": (
        f"[BorrowBox eBooks and eAudiobooks]({BORROWBOX}) - no subscription needed, "
        "available tonight - often hold titles not in the physical collection. "
        f"The [Ask for a Book]({ASK_FOR_A_BOOK_URL}) service can also suggest "
        "alternatives you might enjoy."
    ),
    "url": f"{GOV}/council-services/libraries",
}

CARD_EXPIRED = {
    "summary": (
        "If your Worcestershire library card or membership has expired, you can "
        "renew it free of charge at any library branch. Your account, loans and "
        "reservations are all retained."
    ),
    "what_you_need": (
        "Your library card or card number. If your address has changed since you "
        "joined, bring proof of your current address (e.g. a utility bill, bank "
        "statement or official letter with your name and address)."
    ),
    "how_to": [
        "Visit any Worcestershire library during staffed hours and ask staff to "
        "renew your membership - it only takes a few minutes.",
        "If your address is unchanged, your card number is usually all that's needed.",
        f"Can't visit? Call **{LIBRARY_PHONE}** during staffed hours - staff can "
        "often renew your membership over the phone if your details are on record.",
    ],
    "also_see": (
        f"Not sure if your card has expired? Try logging in to "
        f"[your library account]({ACCOUNT_URL}) - an expired card will prompt you "
        "to renew. If you've also forgotten your PIN, use 'Forgotten PIN' on the "
        "sign-in page."
    ),
    "url": JOIN_URL,
}


CHILDREN_MEMBERSHIP = {
    "summary": (
        "Children can join Worcestershire Libraries at any age - even as babies! "
        "Membership is completely free and gives access to the full range of children's "
        "books, eBooks, audiobooks, Storytime, Rhymetime and the Summer Reading Challenge. "
        "Children under 16 need a parent or guardian to sign the membership form."
    ),
    "what_you_need": (
        "For children under 16: a parent or guardian must be present and sign the form. "
        "Bring the child's name and your home address - proof of address may be requested. "
        "For 16- and 17-year-olds: can join independently with proof of address. "
        "Membership is free for all ages."
    ),
    "how_to": [
        "Visit any Worcestershire library with the child - staff sign you up on the spot "
        "in a few minutes, and the card is usually ready the same day.",
        f"Or [join online]({JOIN_URL}) - a physical card is posted out (or collect in branch).",
        f"**Instant digital membership** is also free and available online right now - "
        f"unlocks [BorrowBox]({BORROWBOX}) eBooks and audiobooks immediately. "
        "Perfect while the physical card arrives.",
    ],
    "also_see": (
        f"Once joined, look out for **Storytime** and **Rhymetime** - free weekly sessions "
        f"at many branches. The **[Summer Reading Challenge]({SUMMER_READING_URL})** "
        "runs July–September and rewards children for reading six library books."
    ),
    "url": JOIN_URL,
}

SUGGEST_PURCHASE = {
    "summary": (
        "If the library doesn't stock a book or item you'd like to borrow, you can "
        "suggest that the library buys it. Library staff review all suggestions and "
        "purchase titles that fit the collection - popular suggestions are given priority."
    ),
    "what_you_need": (
        "No membership required to make a purchase suggestion. If the library orders the "
        "item, you'll need a free library card to borrow it when it arrives."
    ),
    "how_to": [
        f"Use the **[Ask for a Book form]({ASK_FOR_A_BOOK_URL})** - in the free-text "
        "field, explain that you'd like the library to stock a specific title. "
        "Include the title, author, publisher and ISBN if possible.",
        "Or speak to staff at any library branch - they can note your suggestion on the "
        "spot and pass it to the stock team.",
        "You'll be contacted once a decision has been made. If the library orders it, "
        "you'll often be first on the reservation list.",
    ],
    "also_see": (
        f"Need it sooner? If another library service holds it, staff may be able to "
        f"arrange an **inter-library loan** (a small charge usually applies - ask in any branch). "
        f"Or check [BorrowBox]({BORROWBOX}) tonight - thousands of eBooks and audiobooks "
        "included with your library card, no extra cost."
    ),
    "url": ASK_FOR_A_BOOK_URL,
}

LIBRARY_CONTACT = {
    "summary": (
        f"The main Worcestershire Libraries enquiry line is **{LIBRARY_PHONE}** - "
        "for general questions, computer bookings, Libraries Unlocked inductions, "
        "mobile library enquiries and any other library service question."
    ),
    "how_to": [
        f"**Phone:** **{LIBRARY_PHONE}** - during staffed library hours "
        "(hours vary by branch; most are open Monday–Saturday).",
        f"**Online:** Browse the "
        f"[Worcestershire Libraries website]({GOV}/council-services/libraries) - "
        "each service page has relevant contact details and online forms.",
        f"**In person:** Drop in to any "
        f"[Worcestershire library branch]({GOV}/council-services/libraries/find-library) "
        "during staffed hours - staff can answer most questions on the spot.",
        f"**Account queries:** Sign in at [your library account]({ACCOUNT_URL}) "
        "to manage loans, reservations and PIN online, any time.",
    ],
    "also_see": (
        "For a specific branch phone number and opening hours, ask me "
        "'Is [branch name] library open?' or 'What are [branch] library hours?'."
    ),
    "url": f"{GOV}/council-services/libraries",
}

LIBRARY_APP = {
    "summary": (
        "There is no single dedicated 'Worcestershire Libraries' app, but several "
        "apps connect you to library services:"
    ),
    "apps": [
        f"**BorrowBox** - the library's eBook and eAudiobook app. A typical audiobook "
        f"subscription is ~£15/month; yours at no extra cost with your library card. "
        f"Search 'BorrowBox' in the App Store or Google Play, log in with your card number "
        f"and PIN. [Get started]({BORROWBOX})",
        "**PressReader** - free access to 7,000+ newspapers and magazines worldwide "
        "(Guardian, Times, local titles and more). Log in with your library card number on "
        f"the PressReader app or website. [PressReader via Online Hub]({ONLINE_HUB})",
        f"**Your library account (mobile browser)** - manage loans, reservations and renewals "
        f"at [your library account]({ACCOUNT_URL}) - works on any smartphone browser.",
        f"**Libraries Unlocked** - 15+ members use their smartphone to unlock participating "
        f"libraries outside staffed hours. A one-off in-person induction is required first. "
        f"[Libraries Unlocked]({UNLOCKED_URL})",
    ],
    "url": ONLINE_HUB,
    "borrowbox_url": BORROWBOX,
    "account_url": ACCOUNT_URL,
}


LIBRARY_CLOSURES = {
    "summary": (
        "Worcestershire Libraries publish all planned closures for the year online. "
        "Most closures fall on UK bank holidays; individual branches may also close "
        "temporarily for refurbishment or local events."
    ),
    "bank_holiday_note": (
        "Libraries generally close on all UK bank holidays - New Year's Day, Good Friday, "
        "Easter Monday, Early May bank holiday, Spring bank holiday, Summer bank holiday, "
        "Christmas Day and Boxing Day. Opening hours may be reduced on adjacent days."
    ),
    "how_to_check": [
        f"View the full **[2026 library closing dates]({CLOSING_DATES_URL})** page - "
        "updated whenever new closures are confirmed.",
        "Ask me _\"Is [branch name] library open?\"_ or _\"Is [branch] open today?\"_ "
        "for a live status check.",
        f"Call the library enquiry line: **{LIBRARY_PHONE}**.",
    ],
    "url": CLOSING_DATES_URL,
}

BUILDING_ACCESSIBILITY = {
    "summary": (
        "Worcestershire Libraries aim to make all branches as accessible as possible. "
        "Most libraries have step-free entrance, accessible toilets, hearing induction "
        "loops, and enlarged-text self-service terminals."
    ),
    "common_features": [
        "**Step-free access** - ground-floor entrance or lift at the majority of branches.",
        "**Hearing induction loop** - fitted in most library service areas.",
        "**Accessible toilet** - available at most branches.",
        "**Accessible parking** - disabled badge spaces close to the entrance at most sites.",
        "**Large-print signage** and adjustable-font catalogue terminals.",
        "**Self-service kiosks** - usable from a seated position at most branches.",
    ],
    "note": (
        "Specific facilities vary by branch. Ask me _\"which libraries have wheelchair "
        "access?\"_ or _\"libraries with a hearing loop\"_ to find branches that match "
        "your needs, or call ahead to confirm before travelling."
    ),
    "accessible_formats_tip": (
        "For accessible reading formats - large print, DAISY audiobooks, Talking Books "
        "or eAudiobooks - ask me about **'accessible formats'**."
    ),
    "url": f"{GOV}/council-services/libraries/find-library",
    "contact": LIBRARY_PHONE,
}


COMMUNITY_INFORMATION = {
    "summary": (
        "Worcestershire Libraries are community hubs - most branches have "
        "noticeboards where local groups and organisations can display leaflets "
        "and notices, and staff can signpost visitors to local support services."
    ),
    "what_we_offer": [
        "**Community noticeboards** - free for local groups, charities and "
        "community organisations to display information at most branches. "
        "Ask staff at your local library to find out how to post a notice.",
        "**Leaflet displays** - many branches carry leaflets from local "
        "public services, health providers, housing support and charities.",
        "**Signposting** - library staff are trained to help connect you with "
        "local support (food banks, advice services, mental health, housing). "
        "If the library can't help directly, they'll point you to someone who can.",
        "**Warm spaces** - all Worcestershire libraries are warm welcome spaces: "
        "free, no need to buy anything or be a member.",
        "**Free Wi-Fi and computers** - use our free public Wi-Fi or book a "
        "library computer to access online services and community information.",
    ],
    "for_organisations": (
        "Want your group's information displayed in a library? Ask at your nearest "
        f"branch, or call **{LIBRARY_PHONE}** to discuss how to share information "
        "with library visitors."
    ),
    "note": (
        "Libraries do not run or endorse external services - signposting is "
        "provided as a public information resource."
    ),
    "url": f"{GOV}/council-services/libraries",
}

MULTILINGUAL_RESOURCES = {
    "summary": (
        "Worcestershire Libraries offer resources in many languages - "
        "through multilingual digital content, community-language books, "
        "and digital skills support for English language learners."
    ),
    "what_we_offer": [
        f"**Newspapers & magazines in 60+ languages** via [PressReader]({ONLINE_HUB}) - "
        "typically £9.99/month, included with your library card. "
        "Includes Arabic, Chinese (Simplified & Traditional), "
        "French, German, Hindi, Polish, Portuguese, Spanish, Urdu and many more. "
        "Access at home or on library Wi-Fi.",
        "**Books in community languages** - Worcestershire Libraries stock books in a "
        "range of community languages at selected branches. Search the "
        f"[online catalogue]({CATALOGUE_SEARCH}) by language, or ask staff "
        "who can check what's available and request items from other branches.",
        "**eBooks in other languages** - [BorrowBox]({BORROWBOX}) includes eBooks and "
        "eAudiobooks in multiple languages. Search by language in the app.",
        "**Times Digital Archive & Oxford resources** - available in English only, "
        "but [Access to Research]({ONLINE_HUB}) covers international academic journals "
        "in many languages.",
        f"**Digital skills and ESOL support** - our [Digital Inclusion]"
        f"({DIGITAL_INCLUSION_URL}) team can help with English language learning apps "
        "and online resources. Free sessions at many branches.",
    ],
    "joining": (
        f"Library membership is free and open to all Worcestershire residents - "
        f"[join online]({JOIN_URL}) or in any branch. "
        "Under-16s need a parent or guardian."
    ),
    "url": ONLINE_HUB,
    "pressreader_url": ONLINE_HUB,
    "catalogue_url": CATALOGUE_SEARCH,
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
    if (("pin" in q or "password" in q)
            and any(w in q for w in ("forgot", "forgotten", "reset", "change", "new", "lost"))):
        out["focus"] = "pin_reset"
    elif (any(w in q for w in ("reservation", "reserve", "hold", "collect"))
          and any(w in q for w in ("how long", "expire", "expir", "when", "time",
                                   "how many days", "7 day", "collect", "missed"))):
        out["focus"] = "reservations"
    elif any(w in q for w in ("loan limit", "loan period", "borrowing limit",
                              "how long", "how many", "how many books", "how many items")):
        out["focus"] = "loan_limits"
    elif any(w in q for w in ("job club", "cv help", "cv writing", "job search",
                              "job seeking", "job-seeking", "employment support",
                              "find work", "looking for work", "interview prep")):
        out["focus"] = "job_clubs"
        out["job_clubs"] = JOB_CLUBS
    elif ((any(w in q for w in ("expired", "lapsed", "out of date"))
           and any(w in q for w in ("card", "membership", "member")))
          or (any(w in q for w in ("renew", "renewal"))
              and any(w in q for w in ("card", "membership", "member"))
              and not any(w in q for w in ("book", "item", "loan", "dvd", "cd")))):
        out["focus"] = "card_expired"
        out["card_expired"] = CARD_EXPIRED
    elif any(w in q for w in ("renew", "renewal", "extend", "due date")):
        out["focus"] = "renewals"
    elif ("card" in q and any(w in q for w in ("lost", "stolen", "replace", "replacement"))):
        out["focus"] = "lost_card"
        out["lost_card"] = LOST_CARD
    elif (any(w in q for w in ("lost", "damage", "damaged"))
          and any(w in q for w in ("book", "item", "dvd", "cd"))
          and "card" not in q):
        out["focus"] = "lost_item"
    elif any(w in q for w in ("self service", "self-service", "kiosk",
                               "self checkout", "self-checkout", "self serve",
                               "check out myself", "check books out")):
        out["focus"] = "self_service"
        out["self_service"] = SELF_SERVICE
    elif any(w in q for w in ("return", "returning", "bring back", "drop off", "hand back")):
        out["focus"] = "returning"
    elif any(w in q for w in ("inter-library", "interlibrary", "inter library",
                               "another library service", "from another library",
                               "different library service", "ill loan")):
        out["focus"] = "ill"
        out["ill_service"] = ILL_SERVICE
    elif any(w in q for w in ("fine", "charge", "fee", "owe", "overdue", "late", "pay")):
        out["focus"] = "fines"
    elif any(w in q for w in ("reserve", "hold", "reservation", "request", "order")):
        out["focus"] = "reservations"
    elif (any(w in q for w in ("update", "change", "amend", "correct"))
          and any(w in q for w in ("address", "detail", "contact", "email",
                                   "phone", "mobile", "name"))
          and any(w in q for w in ("my", "account", "library"))):
        out["focus"] = "update_details"
        out["update_details"] = UPDATE_DETAILS_INFO
    elif (any(w in q for w in ("wifi", "wi-fi", "wireless"))
          and any(w in q for w in ("connect", "connection", "password", "how",
                                   "access", "guest", "login", "log in", "use"))):
        out["focus"] = "wifi"
        out["wifi_access"] = WIFI_ACCESS
    elif any(w in q for w in ("account", "login", "log in", "sign in", "pin", "password")):
        out["focus"] = "account"
    elif any(w in q for w in ("home", "housebound", "deliver")):
        out["focus"] = "home"
    elif any(w in q for w in ("recommend", "ask for a book", "personalised",
                              "suggest a book", "suggestion", "what should i read",
                              "choose a book", "pick a book")):
        out["focus"] = "ask_book"
    elif any(w in q for w in ("computer", "pc session", "book a computer",
                              "use a computer", "computer session")):
        out["focus"] = "computer"
    elif any(w in q for w in ("volunteer", "volunteering", "work experience")):
        out["focus"] = "volunteer"
    elif any(w in q for w in ("digital skill", "get online", "learn my way",
                              "digital champion", "computer skill", "learn to use")):
        out["focus"] = "digital_skills"
    elif any(w in q for w in ("reading well", "wellbeing book", "mental health book",
                              "books for mental health", "books for wellbeing")):
        out["focus"] = "reading_well"
    elif any(w in q for w in ("room hire", "hire a room", "meeting room",
                              "book a room", "room for hire")):
        out["focus"] = "room_hire"
    elif any(w in q for w in ("warm space", "warm welcome", "somewhere warm")):
        out["focus"] = "warm_space"
    elif any(w in q for w in ("book club", "reading group", "reading circle",
                              "book group", "readers group", "readers' group")):
        out["focus"] = "book_clubs_reading"
        out["book_clubs_reading"] = BOOK_CLUBS
    elif any(w in q for w in ("adult learn", "learning course", "it course",
                              "computer course", "upskill", "functional skill",
                              "learn to read", "learn to write", "learn to type")):
        out["focus"] = "adult_learning"
        out["adult_learning"] = ADULT_LEARNING
    elif any(w in q for w in ("donat", "give books", "drop off book")):
        out["focus"] = "donations"
        out["donations"] = DONATIONS
    elif any(w in q for w in ("school visit", "group visit", "class visit",
                              "school tour", "school trip", "group booking",
                              "school group", "class trip")):
        out["focus"] = "school_visits"
        out["school_visits"] = SCHOOL_VISITS
    elif any(w in q for w in ("accessible format", "accessible reading", "braille",
                              "print disability", "accessible book", "accessible edition",
                              "daisy format", "daisy book", "rnib talking")):
        out["focus"] = "accessible_formats"
        out["accessible_formats"] = ACCESSIBLE_FORMATS
    elif (re.search(r"\b(baby|infant|child|children|kid|kids|toddler|junior|teen|"
                    r"teenager|youth|family)\b", q)
          and any(w in q for w in ("join", "member", "membership", "card",
                                   "sign up", "sign-up", "register", "get a card",
                                   "get their card", "get a library"))):
        out["focus"] = "child_membership"
        out["child_membership"] = CHILDREN_MEMBERSHIP
    elif any(w in q for w in ("library buy", "library stock", "library purchase",
                               "library order this", "ask library to buy",
                               "suggest purchase", "suggest a title",
                               "suggest the library", "buy it for the library",
                               "buy this for the library", "buy a copy for the library",
                               "request library buy", "want library to buy",
                               "library get this book", "wish list")):
        out["focus"] = "suggest_purchase"
        out["suggest_purchase"] = SUGGEST_PURCHASE
    elif any(w in q for w in ("contact library", "library contact", "library phone",
                               "library telephone", "library helpline", "library enquir",
                               "library email", "speak to library", "ring library",
                               "call library", "how do i contact", "how can i contact",
                               "general enquiry", "general enquiries", "reach library",
                               "library number", "main number", "enquiry line")):
        out["focus"] = "library_contact"
        out["library_contact"] = LIBRARY_CONTACT
    elif any(w in q for w in ("library closures", "closing dates", "bank holiday",
                               "closed christmas", "closed easter", "closed bank",
                               "when are libraries closed", "when is the library closed",
                               "library closed", "2026 closing", "planned closure",
                               "refurbishment", "temporary closure")):
        out["focus"] = "library_closures"
        out["library_closures"] = LIBRARY_CLOSURES
    elif any(w in q for w in ("wheelchair", "step-free", "step free", "hearing loop",
                               "induction loop", "building access", "accessible building",
                               "accessible entrance", "disabled access", "disability access",
                               "accessible toilet", "accessible parking", "disabled parking")):
        out["focus"] = "building_accessibility"
        out["building_accessibility"] = BUILDING_ACCESSIBILITY
    elif any(w in q for w in ("teen", "teenager", "young adult", "ya books", "ya fiction",
                               "ya section", "young people library", "youth service",
                               "teen service", "teen activit", "teen event")):
        out["focus"] = "teen_services"
        out["teen_services"] = TEEN_SERVICES
    elif any(w in q for w in ("library app", "app for library", "mobile app", "phone app",
                               "borrowbox app", "pressreader app", "library on my phone",
                               "library on phone", "download library")):
        out["focus"] = "library_app"
        out["library_app"] = LIBRARY_APP
    elif any(w in q for w in ("noticeboard", "notice board", "community notice",
                               "community information", "local group", "advertise",
                               "display leaflet", "signpost", "local service",
                               "food bank", "community hub")):
        out["focus"] = "community_information"
        out["community_information"] = COMMUNITY_INFORMATION
    elif any(w in q for w in ("other language", "non-english", "multilingual",
                               "books in", "language book", "foreign language",
                               "arabic book", "polish book", "urdu book", "hindi book",
                               "spanish book", "french book", "chinese book",
                               "community language", "esol", "english as a second")):
        out["focus"] = "multilingual"
        out["multilingual"] = MULTILINGUAL_RESOURCES
    else:
        out["focus"] = "general"

    if out["focus"] == "home" or any(
            w in q for w in ("home library", "housebound", "deliver")):
        out["home_library"] = HOME_LIBRARY_SERVICE

    if out["focus"] == "ask_book":
        out["ask_book"] = ASK_FOR_A_BOOK
    if out["focus"] == "computer":
        out["computer_booking"] = COMPUTER_BOOKING
    if out["focus"] == "volunteer":
        out["volunteering"] = VOLUNTEERING
    if out["focus"] == "digital_skills":
        out["digital_skills"] = DIGITAL_SKILLS
    if out["focus"] == "reading_well":
        out["reading_well"] = READING_WELL
    if out["focus"] == "room_hire":
        out["room_hire_data"] = ROOM_HIRE
    if out["focus"] == "warm_space":
        out["warm_space"] = WARM_SPACE
    if out["focus"] == "school_visits":
        out.setdefault("school_visits", SCHOOL_VISITS)
    if out["focus"] == "accessible_formats":
        out.setdefault("accessible_formats", ACCESSIBLE_FORMATS)
    if out["focus"] == "update_details":
        out.setdefault("update_details", UPDATE_DETAILS_INFO)
    if out["focus"] == "lost_card":
        out.setdefault("lost_card", LOST_CARD)
    if out["focus"] == "wifi":
        out.setdefault("wifi_access", WIFI_ACCESS)
    if out["focus"] == "self_service":
        out.setdefault("self_service", SELF_SERVICE)
    if out["focus"] == "ill":
        out.setdefault("ill_service", ILL_SERVICE)
    if out["focus"] == "card_expired":
        out.setdefault("card_expired", CARD_EXPIRED)
    if out["focus"] == "child_membership":
        out.setdefault("child_membership", CHILDREN_MEMBERSHIP)
    if out["focus"] == "suggest_purchase":
        out.setdefault("suggest_purchase", SUGGEST_PURCHASE)
    if out["focus"] == "library_contact":
        out.setdefault("library_contact", LIBRARY_CONTACT)
    if out["focus"] == "library_closures":
        out.setdefault("library_closures", LIBRARY_CLOSURES)
    if out["focus"] == "building_accessibility":
        out.setdefault("building_accessibility", BUILDING_ACCESSIBILITY)
    if out["focus"] == "teen_services":
        out.setdefault("teen_services", TEEN_SERVICES)
    if out["focus"] == "library_app":
        out.setdefault("library_app", LIBRARY_APP)
    if out["focus"] == "community_information":
        out.setdefault("community_information", COMMUNITY_INFORMATION)
    if out["focus"] == "multilingual":
        out.setdefault("multilingual", MULTILINGUAL_RESOURCES)

    return out


def children_services() -> dict:
    """Children's library activities - Storytime, Rhymetime, Summer Reading Challenge."""
    return {**CHILDREN_SERVICES, "checked": _now()}


_HUB_SYNONYMS = {
    # Oxford - specific multi-word keys first to avoid "research" matching ebsco
    "oxford english dictionary": "oxford english",
    "oxford english": "oxford english",
    "oxford research encyclopaedias": "oxford research encyclopaedias",
    "oxford research encyclopedia": "oxford research encyclopaedias",
    "oxford research": "oxford research encyclopaedias",
    "oxford reference": "oxford reference",
    "oxford dictionary of national biography": "oxford dictionary of national biography",
    "oxford dnb": "oxford dictionary of national biography",
    "oed": "oxford english",
    "oxford dictionary": "oxford english",
    "encyclopaedia": "oxford research encyclopaedias",
    "encyclopedia": "oxford research encyclopaedias",
    "word history": "oxford english",
    "word meaning": "oxford english",
    # PressReader
    "newspaper": "pressreader", "magazine": "pressreader", "news": "pressreader",
    "press": "pressreader", "guardian": "pressreader", "times newspaper": "pressreader",
    # Ancestry
    "family history": "ancestry", "ancestry": "ancestry", "genealogy": "ancestry",
    "family tree": "ancestry", "genealogical": "ancestry", "census": "ancestry",
    "birth record": "ancestry", "death record": "ancestry", "marriage record": "ancestry",
    # BorrowBox
    "ebook": "borrowbox", "audiobook": "borrowbox", "audio book": "borrowbox",
    # Business / COBRA
    "business": "cobra", "start a business": "cobra", "company": "cobra",
    "sole trader": "cobra", "self employed": "cobra", "self-employed": "cobra",
    "startup": "cobra", "start-up": "cobra",
    # Theory Test Pro
    "driving": "theory test pro", "theory test": "theory test pro",
    "driving test": "theory test pro", "dvsa": "theory test pro",
    "hazard perception": "theory test pro",
    # General Oxford (after specific multi-word entries)
    "dictionary": "oxford english",
    # Research/journals (after oxford research entries)
    "research": "ebsco", "journal": "ebsco",
    # Patents
    "patent": "espacenet", "patents": "espacenet", "intellectual property": "espacenet",
    # BFI
    "film": "bfi", "tv": "bfi", "television": "bfi", "movie": "bfi",
    "british film": "bfi", "archive film": "bfi", "old tv": "bfi",
    "classic film": "bfi", "archive tv": "bfi", "documentary": "bfi",
    # Biography
    "biography": "national biography", "who was": "national biography",
    # Reference
    "reference": "oxford reference",
    # Which?
    "consumer advice": "which", "product review": "which",
    "buying guide": "which", "best buy": "which",
}


def online_hub(topic: str | None = None) -> dict:
    """Free-from-home digital resources (BorrowBox, PressReader, Ancestry…)."""
    hub = kb().get("online_hub", [])
    items = hub
    if topic:
        t = topic.lower()
        hits = [] # synonym-mapped resources rank first
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
    """What you need to sign up - the cross-service membership matrix."""
    out = {"tiers": kb().get("membership_tiers", []),
           "page_url": JOIN_URL,
           "checked": _now()}
    if service:
        s = service.lower()
        if any(w in s for w in ("print", "photocopy")):
            out["need"] = "Full membership + a topped-up PaperCut account."
        elif any(w in s for w in ("ebook", "audiobook", "borrowbox", "online",
                                  "magazine", "newspaper", "ancestry", "digital")):
            out["need"] = "Free digital membership - instant with a postcode."
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
    what status - parsed from the SirsiDynix detail page.

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

    # Strategy 1 - a holdings table (header mentions library/branch + status)
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

    # Strategy 2 - text scan anchored on known branch names
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
    for each, exactly where/how to get it TODAY -
        on the shelf at a named branch (copy-level, live) →
        reserve it (sign in + Place Hold) →
        borrow tonight on BorrowBox (eBook / eAudiobook) →
        not held: ask staff / check the catalogue yourself.
    """
    query = (query or "").strip()
    try:
        res = search_catalogue(query, limit=12)
    except Exception as e: # catalogue unreachable - fail soft, keep the journey
        res = {"items": [], "error": f"catalogue unreachable: {e}"}
    base = {"query": query, "search_url": res.get("search_url",
            CATALOGUE_SEARCH + quote_plus(query)), "checked": _now()}
    if res.get("error") or not res.get("items"):
        base.update({"found": False, "routes": [{
            "route": "not_held",
            "advice": "Nothing matched in the catalogue. Staff can often get "
                      "titles from other library services - ask in any branch, "
                      "or try different search words.",
            "url": base["search_url"],
        }]})
        return base

    items = sorted(res["items"], key=lambda i: _title_sim(query, i["title"]),
                   reverse=True)
    best = items[0]
    cluster = [i for i in items if _title_sim(best["title"], i["title"]) > 0.6]
    routes = []

    # 1) digital first - instant, tonight, no waiting
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
                          "Sign in with your library card number + PIN - or get "
                          "instant digital membership with just a postcode.",
                          "Borrow it free; it auto-returns, so no fines."],
            })

    # 2) physical - copy-level: which branch has it on the shelf right now
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
                          "collect from - any Worcestershire library or the van.",
                          "You'll be emailed when it's ready to collect. "
                          "Reservations are free."],
            })

    if not routes:
        routes.append({"route": "not_held",
                       "advice": "I found similar titles but not that exact one - "
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
# 7. The Hive - page-level KB of thehiveworcester.org (built by build_hive_kb.py)
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
    """Newest catalogue titles for a genre/topic - fuel for a fun 'hot take'."""
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
        print(f" {it['icon']} {it['title']} - {it['author']} [{it['format']} {it['year']}]")

    print("\n### 2. MOBILE LIBRARY: 'abberley' ###")
    res = mobile_library("abberley")
    if "error" in res:
        print(" ", res["error"], "→", res.get("suggestions"))
    else:
        print(f" {res['village']} - {res['date_of_operation']} ({len(res['stops'])} stops)")
        for s in res["stops"][:4]:
            print(f" {s['time']} - {s['location']}")

    print("\n### 2b. MOBILE LIBRARY fuzzy: 'kemsey' (typo) ###")
    res = mobile_library("kemsey")
    print(" ", res.get("village") or res.get("error"), res.get("suggestions", ""))

    print("\n### 3. EVENTS (filter: 'knit') ###")
    res = library_events("knit")
    for e in res["events"]:
        print(f" • {e['name']} - {e['when']} {e['time']} @ {e['location']}")

    print("\n### 3b. EVENTS (all) ###")
    res = library_events()
    print(f" {res['count']} events found")

    print("\n### 4. PRINTING ###")
    res = printing_help()
    print(" ", res["summary"][:80], "...")
    print(" pricing:", res["pricing"])

    print("\n### 5. WHERE TO GET: 'wolf hall' (copy-level availability - LIVE) ###")
    res = where_to_get("wolf hall")
    print(f" found={res['found']} best='{res.get('best_title')}' "
          f"formats={res.get('formats_held')}")
    for r in res.get("routes", []):
        if r["route"] == "shelf":
            spots = ", ".join(f"{c['branch']} ({c['call_number'] or 'ask staff'})"
                              for c in r["copies"][:4])
            print(f" ON SHELF now: {spots}")
        elif r["route"] == "reserve":
            print(f" reserve ({len(r.get('copies', []))} copies tracked, "
                  f"parsed={r.get('availability_parsed')})")
        elif r["route"] == "digital":
            print(f" {r['format']} - direct link: {r['direct_link']}")
        else:
            print(f" {r['route']}: {r.get('advice', '')[:70]}")

    print("\n### 6. HIVE INFO (page-level KB - offline) ###")
    res = hive_info()
    print(f" {res['name']} - {res['opening_hours'][:60]}")
    print(f" pages={res.get('page_count')} capabilities={len(res.get('capabilities', []))} "
          f"as_of={res.get('as_of', '')[:10]}")
    for q in ("archives and old documents", "hire a meeting room", "parking"):
        r = hive_info(q)
        tops = [p["title"] for p in r.get("pages", [])]
        print(f" '{q}' -> pages {tops} + {len(r.get('capabilities', []))} capabilities")
