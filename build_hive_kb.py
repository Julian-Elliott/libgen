"""
build_hive_kb.py - crawl EVERY page of thehiveworcester.org into hive_kb.json.

The Hive is Worcester city's library - Europe's first joint university + public
library - and its website is the only place much of its extended offer is
described (Explore the Past archives & archaeology, 800+ study spaces, room
hire, BIPC business support, the Youth Hub...). We crawl it page-by-page and
record the EXACT offering of each page, with provenance on every fact.

Provenance policy (the re-think of our old "never touch the Hive site" rule):
  • every page object carries its source URL + last_seen date
  • whats-on.html is flagged static/under-construction by the site itself -
    live events still come from worcestershire.gov.uk
  • where the Hive site and the council site conflict (hours, prices,
    membership), the COUNCIL page wins - answers must say which source they used

Output: hive_kb.json
  {generated, source, note, page_count, hive_profile, pages: [...]}

Each page: {url, title, section, summary, offerings[], what_you_need[],
            details{hours,prices,contact,location_in_building}, links_out[],
            last_seen}

`hive_profile.extended_capabilities` is a CURATED list (capability, detail,
source) - re-crawling refreshes every page but PRESERVES the curated list from
the existing hive_kb.json unless you pass --rebuild-capabilities, in which case
a best-effort automatic list is generated for you to re-curate.

Run: python build_hive_kb.py (refresh pages, keep curated profile)
      python build_hive_kb.py --rebuild-capabilities
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone, date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HIVE = "https://www.thehiveworcester.org"
OUT = "hive_kb.json"
HEADERS = {"User-Agent": "WorcsLibrariesKB/3.0 (Hive pages; charitable project)"}
TIMEOUT = 20
DELAY = 0.3
MAX_PAGES = 90

SEEDS = [
    "/", "/collections.html", "/services.html", "/whats-on.html",
    "/plan-your-visit.html", "/learn.html", "/about-the-hive.html",
    "/opening-hours.html", "/getting-here.html", "/contact-us.html",
    "/fines-renewals.html", "/how-to-find-things.html",
    "/borrowing-reservations.html", "/how-to-get-a-library-card.html",
    "/explore-the-past.html", "/book-a-space.html", "/space-for-hire.html",
    "/catalogue-search.html",
]

# URL slug -> nav section. Anything unmatched inherits from its referrer.
SECTION_HINTS = {
    "collections": "collections", "catalogue": "collections",
    "services": "services", "printing": "services", "computer": "services",
    "whats-on": "whats-on", "event": "whats-on",
    "plan-your-visit": "plan-your-visit", "getting-here": "plan-your-visit",
    "opening-hours": "plan-your-visit", "cafe": "plan-your-visit",
    "learn": "learn", "school": "learn", "student": "learn", "youth": "learn",
    "about": "about", "contact": "about", "facts": "about", "creating": "about",
    "library-card": "using-the-library", "borrowing": "using-the-library",
    "fines": "using-the-library", "using-the-library": "using-the-library",
    "how-to-find": "using-the-library", "explore-the-past": "learn",
    "book-a-space": "plan-your-visit", "space-for-hire": "plan-your-visit",
}

NEED_TRIGGERS = re.compile(
    r"\b(you('| wi)?ll need|you need|to (join|use|access|book|register|sign?up)|"
    r"who can|available to|free (to|for)|eligible|membership|library card|"
    r"\bPIN\b|register|sign?up|aged \d+|bring|photo(graphic)? ID|proof of)\b", re.I)

HOURS_RE = re.compile(
    r"((?:open\s+)?(?:from\s+)?\d{1,2}[:.]\d{2}\s*(?:am|pm)?\s*(?:-|to|–)\s*"
    r"\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)[^.]{0,60})", re.I)
PRICE_RE = re.compile(r"((?:£\d+(?:\.\d{2})?|\d+p)\b[^.]{0,80})")
PHONE_RE = re.compile(r"(0\d{4}\s?\d{6}|\d{5}\s\d{6})")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LEVEL_RE = re.compile(r"\b([Ll]evel \d)\b")


def canon(url: str) -> str:
    url = urljoin(HIVE + "/", url).split("#")[0].split("?")[0]
    url = url.replace("http://", "https://").replace("://thehive", "://www.thehive")
    if url.rstrip("/") in (HIVE, HIVE + "/index.html"):
        return HIVE + "/"
    return url


def is_internal_page(url: str) -> bool:
    p = urlparse(url)
    return (p.netloc == "www.thehiveworcester.org"
            and (p.path.endswith(".html") or p.path in ("", "/")))


def section_of(url: str, referrer_section: str) -> str:
    slug = urlparse(url).path.lower()
    for hint, sec in SECTION_HINTS.items():
        if hint in slug:
            return sec
    return referrer_section or "other"


def main_text(soup: BeautifulSoup):
    main = (soup.find("main") or soup.find(id="content")
            or soup.find(class_="container") or soup.body or soup)
    for bad in main.select("nav, header, footer, script, style,.navbar,.carousel"):
        bad.decompose()
    return main


def extract_page(url: str, html: str, referrer_section: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = main_text(soup)
    h1 = main.find(["h1", "h2"])
    title = (h1.get_text(" ", strip=True) if h1 else
             (soup.title.get_text(strip=True).strip(" -") if soup.title else url))

    paras = [re.sub(r"\s+", " ", p.get_text(" ", strip=True))
             for p in main.find_all("p")]
    paras = [p for p in paras if len(p) > 30]
    summary = paras[0][:300] if paras else ""

    # EXACT offerings: headings + list items + lead sentences, near-verbatim.
    offerings, seen = [], set()
    for el in main.find_all(["h2", "h3", "h4", "li"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if 4 < len(t) <= 200 and t.lower() not in seen:
            seen.add(t.lower())
            offerings.append(t)
    for p in paras[:6]:
        first = p.split(". ")[0].strip()
        if 15 < len(first) <= 200 and first.lower() not in seen:
            seen.add(first.lower())
            offerings.append(first)

    blob = " ".join(paras)
    need = []
    for m in re.finditer(r"[^.!?]*[.!?]", blob):
        seg = m.group(0).strip()
        if 15 <= len(seg) <= 220 and NEED_TRIGGERS.search(seg):
            if seg.lower() not in (n.lower() for n in need):
                need.append(seg)
        if len(need) >= 5:
            break

    details = {}
    if (m:= HOURS_RE.search(blob)):
        details["hours"] = m.group(1).strip()
    prices = PRICE_RE.findall(blob)[:6]
    if prices:
        details["prices"] = "; ".join(p.strip() for p in prices)
    contact = PHONE_RE.findall(blob)[:2] + EMAIL_RE.findall(blob)[:3]
    if contact:
        details["contact"] = ", ".join(dict.fromkeys(contact))
    if (m:= LEVEL_RE.search(blob)):
        details["location_in_building"] = m.group(1)

    links_out = sorted({a["href"] for a in main.find_all("a", href=True)
                        if a["href"].startswith("http")
                        and "thehiveworcester.org" not in a["href"]})[:15]

    page = {"url": url, "title": title,
            "section": section_of(url, referrer_section),
            "summary": summary, "offerings": offerings[:30],
            "last_seen": date.today().isoformat()}
    if need:
        page["what_you_need"] = need
    if details:
        page["details"] = details
    if links_out:
        page["links_out"] = links_out
    if "whats-on" in url:
        page["notes"] = ("Site flags this page as static/under construction - "
                         "use worcestershire.gov.uk for live events.")
    return page, soup


def auto_capabilities(pages: list[dict]) -> list[dict]:
    """Best-effort automatic capability list (for --rebuild-capabilities)."""
    KEY = ("archive", "archaeolog", "study space", "business", "youth",
           "children", "room", "hire", "university", "special collection",
           "research", "café", "cafe", "catalogue")
    out = []
    for p in pages:
        hits = [o for o in p.get("offerings", [])
                if any(k in o.lower() for k in KEY)]
        for h in hits[:2]:
            out.append({"capability": p["title"], "detail": h, "source": p["url"]})
    return out[:40]


def main():
    rebuild_caps = "--rebuild-capabilities" in sys.argv

    # preserve the curated profile unless told otherwise
    old_profile = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            old_profile = json.load(f).get("hive_profile", {})
    except FileNotFoundError:
        pass

    queue = [(canon(s), "other") for s in SEEDS]
    done: dict[str, dict] = {}
    while queue and len(done) < MAX_PAGES:
        url, ref_sec = queue.pop(0)
        if url in done:
            continue
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            done[url] = {"url": url, "error": str(e),
                         "last_seen": date.today().isoformat()}
            continue
        page, soup = extract_page(url, r.text, ref_sec)
        done[url] = page
        for a in soup.find_all("a", href=True):
            u = canon(a["href"])
            if is_internal_page(u) and u not in done:
                queue.append((u, page["section"]))
        if len(done) % 10 == 0:
            print(f"...{len(done)} pages")
        time.sleep(DELAY)

    pages = sorted(done.values(), key=lambda p: p["url"])
    profile = old_profile if (old_profile and not rebuild_caps) else {
        "name": "The Hive, Worcester",
        "extended_capabilities": auto_capabilities(pages),
        "note": "Auto-generated - please re-curate against the pages.",
    }

    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": HIVE,
        "method": "requests BFS crawl, every internal.html page",
        "note": ("Built page-by-page from thehiveworcester.org. Every fact "
                 "carries its source page. Where the Hive site and "
                 "worcestershire.gov.uk conflict (hours, prices, membership), "
                 "the council page wins; the site itself flags whats-on.html "
                 "as static/under-construction."),
        "page_count": len(pages),
        "hive_profile": profile,
        "pages": pages,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"wrote {OUT}: {len(pages)} pages, "
          f"{len(profile.get('extended_capabilities', []))} capabilities "
          f"({'auto' if rebuild_caps or not old_profile else 'curated, preserved'})")


if __name__ == "__main__":
    main()
