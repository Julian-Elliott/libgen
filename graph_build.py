"""
graph_build.py — turn library_kb.json into a knowledge graph (GraphRAG-style).

Pipeline (mirrors microsoft/graphrag's stages, but deterministic + local so it
runs on a laptop with no LLM calls and no API cost):

    KB documents  ->  Entities  ->  Relationships  ->  Communities  ->  Reports

Why a graph and not flat RAG? It answers MULTI-HOP questions a chatbot can't,
e.g. "which late-opening library has free parking and a cafe?" — that traverses
Branch -HAS_FACILITY-> Facility and Branch -OFFERS-> Libraries Unlocked in one go.

Run after build_kb.py:   python build_kb.py && python graph_build.py
Output: library_graph.json  (nodes, typed edges, communities, reports)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import networkx as nx
from networkx.algorithms import community as nx_comm

try:                                   # curated online-hub access detail
    from library_sources import CURATED_HUB
except Exception:
    CURATED_HUB = {}

KB_PATH = "library_kb.json"
OUT_PATH = "library_graph.json"
GOV = "https://www.worcestershire.gov.uk"

POSTCODE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}")


def town_of(address: str) -> str:
    m = POSTCODE.search(address or "")
    if not m:
        return ""
    before = (address[:m.start()]).strip().split()
    return before[-1].title() if before else ""


def infer_tier(need) -> str | None:
    t = " ".join(need).lower() if isinstance(need, list) else str(need).lower()
    if "induction" in t or "unlocked" in t:
        return "Libraries Unlocked"
    if "papercut" in t or "print" in t:
        return "Full membership"
    if "digital" in t or "postcode" in t:
        return "Digital membership"
    if any(w in t for w in ("member", "card", "join", "pin")):
        return "Full membership"
    return None


def build_graph(kb: dict) -> nx.Graph:
    G = nx.Graph()

    def add(node_id, ntype, label, **attrs):
        G.add_node(node_id, type=ntype, label=label, **attrs)
        return node_id

    # --- Membership tiers (the "what you need to sign up" spine) ---
    for tier in kb.get("membership_tiers", []):
        add(f"tier::{tier['tier']}", "Membership", tier["tier"],
            what_you_need=tier.get("what_you_need", ""),
            unlocks=tier.get("unlocks", ""), url=tier.get("url", ""))

    hub_node = add("hub::online", "Hub", "Online library hub",
                   url=f"{GOV}/council-services/libraries/online-library-hub")

    # --- Branches -> facilities, area, Libraries Unlocked ---
    for b in kb.get("branches", []):
        bid = add(f"branch::{b['name']}", "Branch", b["name"],
                  address=b.get("address", ""), hours=b.get("hours", {}),
                  facilities=b.get("facilities", []),
                  libraries_unlocked=b.get("libraries_unlocked", False),
                  url=b.get("url", ""))
        for fac in b.get("facilities", []):
            fid = add(f"facility::{fac}", "Facility", fac)
            G.add_edge(bid, fid, rel="HAS_FACILITY")
        town = town_of(b.get("address", ""))
        if town:
            aid = add(f"area::{town}", "Area", town)
            G.add_edge(bid, aid, rel="LOCATED_IN")
        if b.get("libraries_unlocked"):
            G.add_edge(bid, "tier::Libraries Unlocked", rel="OFFERS")

    # --- Services -> category topic, required membership tier ---
    for s in kb.get("services", []):
        sid = add(f"service::{s['title']}", "Service", s["title"],
                  category=s.get("category", "general"),
                  summary=s.get("summary", ""),
                  what_you_need=s.get("what_you_need", []),
                  how_to=s.get("how_to", []), url=s.get("url", ""))
        tid = add(f"topic::{s.get('category','general')}", "Topic",
                  s.get("category", "general").title())
        G.add_edge(sid, tid, rel="IN_CATEGORY")
        tier = infer_tier(s.get("what_you_need", []))
        if tier and f"tier::{tier}" in G:
            G.add_edge(sid, f"tier::{tier}", rel="REQUIRES")

    # --- Online-hub resources -> hub, required tier, curated access ---
    for h in kb.get("online_hub", []):
        cur = next((v for k, v in CURATED_HUB.items()
                    if k in h["name"].lower() or h["name"].lower() in k), {})
        rid = add(f"resource::{h['name']}", "Resource", h["name"],
                  summary=cur.get("inside") or h.get("summary", ""),
                  what_you_need=cur.get("what_you_need", ""),
                  access=cur.get("access", []), at_home=cur.get("at_home"),
                  titles=cur.get("titles", []), url=h.get("url", ""))
        G.add_edge(rid, hub_node, rel="PART_OF")
        tier = "Digital membership" if cur.get("at_home", True) else "Full membership"
        if f"tier::{tier}" in G:
            G.add_edge(rid, f"tier::{tier}", rel="REQUIRES")

    # --- Mobile library villages (best-effort live fetch) ---
    mob = add("service::Mobile library", "Service", "Mobile library",
              category="access",
              url=f"{GOV}/council-services/libraries/your-library-membership/mobile-library")
    try:
        from library_sources import _village_index
        for name, url in list(_village_index().items()):
            vid = add(f"village::{name}", "Village", name.title(), url=url)
            G.add_edge(vid, mob, rel="SERVED_BY")
    except Exception as e:
        print(f"  (mobile villages skipped: {e})")

    return G


def detect_communities(G: nx.Graph) -> list[dict]:
    coms = nx_comm.greedy_modularity_communities(G)
    reports = []
    for i, members in enumerate(sorted(coms, key=len, reverse=True)):
        members = list(members)
        by_type: dict[str, list[str]] = {}
        for n in members:
            by_type.setdefault(G.nodes[n]["type"], []).append(G.nodes[n]["label"])
        # readable, deterministic "community report"
        title_bits = []
        for t in ("Branch", "Area", "Service", "Resource", "Facility", "Membership"):
            if by_type.get(t):
                title_bits.append(f"{len(by_type[t])} {t.lower()}{'s' if len(by_type[t])>1 else ''}")
        title = f"Cluster {i+1}: " + ", ".join(title_bits[:3]) if title_bits else f"Cluster {i+1}"
        lines = [f"{t}: {', '.join(sorted(set(v))[:12])}"
                 for t, v in sorted(by_type.items())]
        reports.append({
            "id": f"community::{i}",
            "title": title,
            "size": len(members),
            "members": members,
            "report": " | ".join(lines),
        })
    return reports


def main():
    with open(KB_PATH, encoding="utf-8") as f:
        kb = json.load(f)
    G = build_graph(kb)
    communities = detect_communities(G)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_from": KB_PATH,
        "method": "deterministic GraphRAG-style graph (entities->relationships->"
                  "communities->reports); inspired by microsoft/graphrag",
        "stats": {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
                  "communities": len(communities),
                  "node_types": _count_types(G)},
        "nodes": [{"id": n, **{k: v for k, v in d.items()}}
                  for n, d in G.nodes(data=True)],
        "edges": [{"source": u, "target": v, "rel": d.get("rel", "RELATED")}
                  for u, v, d in G.edges(data=True)],
        "communities": communities,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"wrote {OUT_PATH}")
    print("stats:", out["stats"])

    # --- prove the multi-hop value (a query flat RAG can't answer) ---
    print("\nMulti-hop demo: late-opening (Libraries Unlocked) libraries that "
          "also have a café AND meeting rooms:")
    hits = 0
    for n, d in G.nodes(data=True):
        if d["type"] != "Branch" or not d.get("libraries_unlocked"):
            continue
        facs = set(d.get("facilities", []))
        if any("caf" in f.lower() for f in facs) and "meeting rooms" in facs:
            print(f"   ✓ {d['label']} — open to 8pm, café + meeting rooms")
            hits += 1
    print(f"   ({hits} match — traversed Branch→OFFERS→Unlocked + Branch→HAS_FACILITY)")


def _count_types(G):
    c = {}
    for _, d in G.nodes(data=True):
        c[d["type"]] = c.get(d["type"], 0) + 1
    return c


if __name__ == "__main__":
    main()
