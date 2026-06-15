"""
build_kb.py - crawl the *entire* Worcestershire Libraries section of
worcestershire.gov.uk and build one current, reliable knowledge base.

Output: library_kb.json with three parts:
  • services - every library service/content page: summary + WHAT YOU NEED
                  to sign up (eligibility) + how-to steps + source URL
  • branches - each library: address, day-by-day hours (core + Libraries
                  Unlocked), facilities (toilets, parking, cafe, wifi...)
  • online_hub - each online resource (BorrowBox, Ancestry...) + what you need

Run: python build_kb.py (re-run any time to refresh - it's all live)

We only ever read worcestershire.gov.uk (the council's own pages). We never
touch thehiveworcester.org - that content is unreliable / out of date.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

GOV = "https://www.worcestershire.gov.uk"
SITEMAP = f"{GOV}/sitemap.xml"
HEADERS = {"User-Agent": "WorcsLibrariesKB/2.0 (hackathon; +council pages only)"}
TIMEOUT = 20
DELAY = 0.25 # be polite

# Pages that are not user-facing services - skip from the KB.
SKIP = re.compile(
    r"/events/|/mobile-library/|privacy-notice|byelaws|reading-pledge-wall|"
    r"worcestershires?-library-stories|/library-stories|service-disruption",
    re.I,
)

FACILITY_VOCAB = {
    "public toilets": r"\bpublic toilets?\b",
    "accessible toilet": r"\b(disabled|accessible) toilets?\b",
    "baby changing": r"\bbaby chang",
    "wheelchair access": r"\bwheelchair|step-?free|level access\b",
    "hearing loop": r"\bhearing loop|induction loop\b",
    "free Wi-Fi": r"\bwi-?fi\b",
    "public computers": r"\b(public )?computers?\b",
    "study space": r"\bstudy (space|area|room)|quiet (space|study)\b",
    "meeting rooms": r"\bmeeting rooms?\b",
    "café": r"\bcaf[eé]\b",
    "parking": r"\bparking\b",
    "self-service": r"\bself-?service\b",
    "printing": r"\bprint(ing)?|photocopy",
}

# Sentence triggers that signal eligibility / "what you need to sign up".
NEED_TRIGGERS = re.compile(
    r"\b(you('| wi)?ll need|you need|to (join|use|access|register|sign?up)|"
    r"who can|available to|free (to|for)|eligible|membership|library card|"
    r"\bPIN\b|register|sign?up|induction|aged \d+|residents?|anyone|upgrade)\b",
    re.I,
)
# Time-bound notices that LOOK like eligibility but aren't (drop them).
NEED_EXCLUDE = re.compile(
    r"\bfrom \d|will be on hand|demonstrate|this (summer|christmas|autumn|spring)|"
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\b|\b20\d\d\b", re.I,
)
# Dated / campaign pages - tag as seasonal so they're not shown as standing services.
SEASONAL = re.compile(
    r"summer-reading-challenge|world-book-day|steamfest|christmas|halloween|"
    r"national-year|young-poet|get-school-ready|reading-pledge|world book day|"
    r"this summer", re.I,
)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def discover() -> list[str]:
    """All unique library URLs from the sitemap (deduped across path prefixes)."""
    idx = _get(SITEMAP)
    pages = sorted(set(re.findall(r"sitemap\.xml\?page=\d+", idx)))
    urls: set[str] = set()
    for p in pages:
        xml = _get(f"{GOV}/{p}")
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            if "librar" in loc.lower():
                urls.add(loc.strip())
    # collapse /libraries/ vs /worcestershire-libraries/ duplicates by slug-tail
    canon: dict[str, str] = {}
    for u in urls:
        key = re.sub(r".*?/(worcestershire-)?libraries/?", "", u).rstrip("/")
        # prefer the shorter, canonical /libraries/ form
        if key not in canon or "/worcestershire-libraries/" not in u:
            canon.setdefault(key, u)
            if "/worcestershire-libraries/" not in u:
                canon[key] = u
    return sorted(canon.values())


def main_text(soup: BeautifulSoup):
    main = soup.find("main") or soup.find(id="main-content") or soup
    for bad in main.select("nav, header, footer, script, style,.breadcrumb, form"):
        bad.decompose()
    return main


def intro(main) -> str:
    p = main.find("p")
    return re.sub(r"\s+", " ", p.get_text(" ", strip=True)) if p else ""


def what_you_need(main) -> list[str]:
    txt = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
    out, seen = [], set()
    for m in re.finditer(r"[^.!?]*\.", txt):
        seg = m.group(0).strip()
        if (15 <= len(seg) <= 220 and NEED_TRIGGERS.search(seg)
                and not NEED_EXCLUDE.search(seg)):
            key = seg.lower()
            if key not in seen:
                seen.add(key)
                out.append(seg)
        if len(out) >= 5:
            break
    return out


def how_to(main) -> list[str]:
    for ol in main.find_all("ol"):
        items = [re.sub(r"\s+", " ", li.get_text(" ", strip=True))
                 for li in ol.find_all("li")]
        items = [i for i in items if 4 < len(i) < 200]
        if 1 < len(items) <= 12:
            return items
    return []


def title_of(soup, url) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return url.rstrip("/").split("/")[-1].replace("-", " ").title()


def parse_hours(main) -> dict[str, dict]:
    """{Day: {"staffed": "...", "unlocked": "..."}} - handles both the
    Libraries-Unlocked column table and plain 'Day: hours' text."""
    # 1) structured table (Libraries Unlocked branches)
    for table in main.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower()
                   for c in rows[0].find_all(["th", "td"])]
        core_idx = next((i for i, h in enumerate(headers)
                         if "core" in h or "staffed" in h), None)
        out: dict[str, dict] = {}
        for tr in rows[1:]:
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                     for c in tr.find_all(["th", "td"])]
            if cells and cells[0].lower() in DAYS:
                staffed = (cells[core_idx] if core_idx and core_idx < len(cells)
                           else (cells[1] if len(cells) > 1 else ""))
                out[cells[0].title()] = {"staffed": staffed,
                                         "unlocked": "8:00am to 8:00pm"}
        if out:
            return out
    # 2) plain-text 'Day: hours' (community libraries)
    txt = main.get_text("\n")
    out = {}
    for m in re.finditer(
        r"(?im)^\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*:"
        r"\s*(.+?)\s*$", txt):
        out[m.group(1).title()] = {"staffed": m.group(2).strip()}
    return out


def parse_branch(soup, url) -> dict:
    main = main_text(soup)
    address = ""
    addr = main.find(class_=re.compile("address|location|adr", re.I))
    if addr:
        address = re.sub(r"\s+", " ", addr.get_text(" ", strip=True))
    hours = parse_hours(main)
    blob = main.get_text(" ", strip=True)
    facilities = [name for name, pat in FACILITY_VOCAB.items()
                  if re.search(pat, blob, re.I)]
    return {
        "name": title_of(soup, url),
        "url": url,
        "address": address,
        "hours": hours,
        "facilities": facilities,
        "libraries_unlocked": bool(re.search(r"libraries unlocked", blob, re.I))
        and any("unlocked" in v for v in hours.values()),
    }


def parse_hub(soup, url) -> dict:
    main = main_text(soup)
    return {
        "name": title_of(soup, url),
        "url": url,
        "summary": intro(main),
        "what_you_need": what_you_need(main),
    }


def parse_service(soup, url) -> dict:
    main = main_text(soup)
    title = title_of(soup, url)
    if SEASONAL.search(url) or SEASONAL.search(title):
        cat = "seasonal"
    else:
        cat = "membership" if "your-library-membership" in url else (
            "learning" if "learn-upskill" in url or "learning-outside" in url else (
            "business" if "business" in url or "bipc" in url else (
            "reading" if "read-and-discover" in url else (
            "wellbeing" if "wellbeing" in url or "warm" in url or "connect" in url
            else "general"))))
    return {
        "title": title,
        "url": url,
        "category": cat,
        "summary": intro(main),
        "what_you_need": what_you_need(main),
        "how_to": how_to(main),
    }


def build() -> dict:
    urls = discover()
    print(f"discovered {len(urls)} unique library URLs")
    branches, hub, services = [], [], []
    for i, u in enumerate(urls, 1):
        if SKIP.search(u):
            continue
        try:
            soup = BeautifulSoup(_get(u), "html.parser")
        except Exception as e:
            print(f"! {u} -> {e}")
            continue
        if re.search(r"/find-library/[a-z]", u):
            branches.append(parse_branch(soup, u))
        elif re.search(r"/online-library-hub/[a-z]", u):
            hub.append(parse_hub(soup, u))
        else:
            services.append(parse_service(soup, u))
        if i % 15 == 0:
            print(f"...{i}/{len(urls)}")
        time.sleep(DELAY)

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": GOV,
        "note": "Council pages only; the Hive website is deliberately excluded.",
        "counts": {"services": len(services), "branches": len(branches),
                   "online_hub": len(hub)},
        "membership_tiers": MEMBERSHIP_TIERS,
        "branches": sorted(branches, key=lambda b: b["name"]),
        "online_hub": sorted(hub, key=lambda h: h["name"]),
        "services": sorted(services, key=lambda s: (s["category"], s["title"])),
    }


# Curated cross-cutting "what you need" matrix - the one view no single page
# gives, verified against the council pages crawled here.
MEMBERSHIP_TIERS = [
    {"tier": "Digital membership",
     "what_you_need": "Just a Worcestershire postcode - sign up online in minutes, no card needed.",
     "unlocks": "Free eBooks, eAudiobooks, eMagazines & eNewspapers (BorrowBox, PressReader), Times Digital Archive, Oxford University Press.",
     "url": f"{GOV}/council-services/libraries/online-library-hub/digital-library-membership"},
    {"tier": "Full membership",
     "what_you_need": "Free for everyone - join online then collect, or join in person at any library. Gives you a library card number + PIN.",
     "unlocks": "Borrow physical books, reserve/renew, use public computers, Print Your Way, the mobile library, and the full online hub.",
     "url": f"{GOV}/council-services/libraries/your-library-membership/join-library"},
    {"tier": "Libraries Unlocked",
     "what_you_need": "Be a full member aged 15+, then do a short one-off induction with staff.",
     "unlocks": "Self-service access 8am-8pm Mon-Sat (even when unstaffed) at 11 branches: Bromsgrove, Droitwich, Evesham, Kidderminster, Malvern, Pershore, Redditch, Rubery, St John's, Stourport, Tenbury.",
     "url": f"{GOV}/council-services/libraries/libraries-unlocked"},
]


if __name__ == "__main__":
    kb = build()
    with open("library_kb.json", "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2, ensure_ascii=False)
    print("\nwrote library_kb.json")
    print("counts:", kb["counts"])
    print("\nsample service:")
    s = next((x for x in kb["services"] if x["what_you_need"]), kb["services"][0])
    print(" ", s["title"], "->", s["what_you_need"][:1])
    print("sample branch:")
    b = kb["branches"][0]
    print(" ", b["name"], "| facilities:", b["facilities"], "| days:", list(b["hours"])[:2])
