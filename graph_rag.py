"""
graph_rag.py — query the knowledge graph built by graph_build.py.

This is the runtime half of our GraphRAG: it answers the MULTI-HOP questions
flat retrieval can't, by traversing typed edges
(Branch-HAS_FACILITY-Facility, Branch-OFFERS-Libraries Unlocked, Service-REQUIRES-tier…).

    local_search  — entity-anchored: "which late library has a café + meeting rooms?"
    global_search — community-level: "what does my library offer overall?"

No LLM here — it returns structured context that app.py's small model phrases.
"""

from __future__ import annotations

import json
import os
import re

GRAPH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "library_graph.json")

# query word -> facility label fragment to match in a branch's facilities list
FACILITY_TERMS = {
    "parking": "parking", "car park": "parking", "café": "caf", "cafe": "caf",
    "coffee": "caf", "wifi": "wi-fi", "wi-fi": "wi-fi", "internet": "wi-fi",
    "computer": "computer", "pc": "computer", "study": "study", "quiet": "study",
    "toilet": "toilet", "loo": "toilet", "baby": "baby", "changing": "baby",
    "wheelchair": "wheelchair", "accessible": "accessible", "disabled": "accessible",
    "meeting room": "meeting", "meeting": "meeting", "print": "printing",
    "photocopy": "printing", "self-service": "self",
}
LATE_TERMS = ["late", "unlocked", "8pm", "evening", "after work", "after hours",
              "open late", "out of hours"]

_G = None


def graph() -> dict:
    global _G
    if _G is None:
        try:
            with open(GRAPH_PATH, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = {"nodes": [], "edges": [], "communities": []}
        nodes = {n["id"]: n for n in raw.get("nodes", [])}
        adj: dict[str, list] = {nid: [] for nid in nodes}
        for e in raw.get("edges", []):
            s, t, rel = e["source"], e["target"], e.get("rel", "RELATED")
            adj.setdefault(s, []).append((t, rel))
            adj.setdefault(t, []).append((s, rel))
        by_type: dict[str, list] = {}
        for n in nodes.values():
            by_type.setdefault(n["type"], []).append(n)
        _G = {"nodes": nodes, "adj": adj, "by_type": by_type,
              "communities": raw.get("communities", []),
              "generated": raw.get("generated", "")}
    return _G


def _area_in_query(q: str) -> str:
    areas = [n["label"] for n in graph()["by_type"].get("Area", [])]
    for a in areas:
        if a.lower() in q:
            return a
    return ""


def local_search(query: str) -> dict:
    """Entity-anchored multi-hop search."""
    g = graph()
    q = (query or "").lower()

    wanted = {frag for term, frag in FACILITY_TERMS.items() if term in q}
    want_late = any(t in q for t in LATE_TERMS)
    area = _area_in_query(q)

    # --- branch filter (the headline multi-hop) ---
    if wanted or want_late or (area and "librar" in q):
        results = []
        for b in g["by_type"].get("Branch", []):
            if want_late and not b.get("libraries_unlocked"):
                continue
            if area and area.lower() not in (b.get("address", "")).lower():
                continue
            facs = b.get("facilities", [])
            if all(any(w in f.lower() for f in facs) for w in wanted):
                results.append(b)
        return {
            "kind": "branch_filter",
            "wanted_facilities": sorted(wanted),
            "late": want_late, "area": area,
            "branches": [{"name": b["label"], "facilities": b.get("facilities", []),
                          "libraries_unlocked": b.get("libraries_unlocked", False),
                          "address": b.get("address", ""), "url": b.get("url", "")}
                         for b in results],
            "count": len(results),
        }

    # --- entity neighbourhood lookup ---
    terms = [w for w in re.findall(r"[a-z]{4,}", q)]
    scored = []
    for nid, n in g["nodes"].items():
        if n["type"] in ("Village",):
            continue
        hay = (n.get("label", "") + " " + str(n.get("summary", ""))).lower()
        score = sum(1 for t in terms if t in hay)
        if n.get("label", "").lower() in q:
            score += 3
        if score:
            scored.append((score, nid, n))
    scored.sort(key=lambda x: -x[0])
    ents = []
    for _, nid, n in scored[:4]:
        neigh = []
        for t, rel in g["adj"].get(nid, [])[:8]:
            tn = g["nodes"].get(t, {})
            neigh.append({"rel": rel, "label": tn.get("label", t),
                          "type": tn.get("type", "")})
        ents.append({"label": n["label"], "type": n["type"],
                     "summary": n.get("summary", ""),
                     "what_you_need": n.get("what_you_need", ""),
                     "url": n.get("url", ""), "related": neigh})
    return {"kind": "entity", "entities": ents, "count": len(ents)}


def global_search(query: str) -> dict:
    """Community-level overview for 'big picture' questions."""
    g = graph()
    terms = set(re.findall(r"[a-z]{4,}", (query or "").lower()))
    scored = []
    for c in g["communities"]:
        hay = (c.get("title", "") + " " + c.get("report", "")).lower()
        scored.append((sum(1 for t in terms if t in hay), c))
    scored.sort(key=lambda x: -x[0])
    return {"kind": "global",
            "communities": [{"title": c["title"], "report": c["report"][:600]}
                            for s, c in scored[:3]]}


def graph_search(query: str) -> dict:
    """Entry point used as an agent tool. Picks local vs global automatically."""
    q = (query or "").lower()
    if any(w in q for w in ("overall", "everything", "what do you offer",
                            "what can", "all the", "in general")):
        res = global_search(query)
    else:
        res = local_search(query)
    res["page_url"] = "https://www.worcestershire.gov.uk/council-services/libraries"
    res["graph_generated"] = graph().get("generated", "")
    return res


if __name__ == "__main__":
    import json as _j
    for q in ["a late-opening library with a café and meeting rooms",
              "which library has study space and free wifi",
              "free wifi in Malvern",
              "tell me about borrowbox",
              "what does my library offer overall"]:
        r = graph_search(q)
        print(f"\nQ: {q}\n  kind={r['kind']}", end="")
        if r["kind"] == "branch_filter":
            print(f" wanted={r['wanted_facilities']} late={r['late']} -> "
                  f"{[b['name'] for b in r['branches']]}")
        elif r["kind"] == "entity":
            print(" ->", [f"{e['label']}({e['type']})" for e in r["entities"]])
        else:
            print(" ->", [c["title"] for c in r["communities"]])
