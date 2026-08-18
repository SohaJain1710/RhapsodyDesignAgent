"""
ad_to_mermaid.py
-----------------
Converts read_detailed_ad.py output (JSON) to a Mermaid flowchart string,
keeping a name→GUID sidecar so patch tools can resolve elements back to GUIDs.

Mermaid node shapes used:
  action    →  rect       ActionName["ActionName\\n[SRS_SDM_x, ...]"]
  decision  →  diamond    DecisionName{DecisionName}
  junction  →  circle     MergeName((MergeName))
  initial   →  stadium    initial([start])
  final     →  stadium    End([end])

The sidecar maps every node name to its GUID and type so callers can
resolve LLM-proposed patch targets back to COM elements.

Usage:
    from ad_to_mermaid import to_mermaid, from_mermaid_node

    mermaid_str, sidecar = to_mermaid(ad_json)
    # pass mermaid_str to LLM
    # after LLM response, use sidecar to resolve GUIDs
"""
from __future__ import annotations
import re


def to_mermaid(ad: dict) -> tuple[str, dict]:
    """
    Convert read_detailed_ad.py output to a Mermaid flowchart string.

    Returns:
        (mermaid_str, sidecar)

        mermaid_str: Mermaid flowchart TD string for LLM input
        sidecar:     dict mapping node_name -> {guid, type, requirements}
                     for resolving LLM patch proposals back to COM GUIDs
    """
    lines  = ["flowchart TD"]
    sidecar = {}

    # ── Node definitions ─────────────────────────────────────────────────
    # Initial node
    initial = ad.get("initial_node")
    if initial:
        lines.append(f'    initial([▶ start])')
        sidecar["initial"] = {
            "guid": initial["guid"],
            "type": "initial",
            "requirements": [],
        }

    # Actions — label shows description only, no requirement IDs
    for a in ad.get("actions", []):
        name     = a["name"]
        reqs     = a.get("requirements", [])
        text     = a.get("entry_action", "").replace('"', "'").replace("\r\n", "\n").replace("\r", "\n").strip()

        label_parts = []
        if text:
            text_lines = [l.strip() for l in text.split("\n") if l.strip()]
            text_lines = [l[:120] + ("..." if len(l) > 120 else "") for l in text_lines]
            label_parts.append("\\n".join(text_lines))

        label = "\\n".join(label_parts) if label_parts else name
        lines.append(f'    {name}["{label}"]')
        alloc = a.get("allocated_module", "")
        if alloc:
            lines.append(f'    %% {name} AllocatedToModule: {alloc}')
        sidecar[name] = {
            "guid"           : a["guid"],
            "type"           : "action",
            "requirements"   : reqs,
            "entry_action"   : text,
            "allocated_module": a.get("allocated_module", ""),
        }

    # Decisions
    for d in ad.get("decisions", []):
        name = d["name"]
        lines.append(f'    {name}{{{name}}}')
        sidecar[name] = {
            "guid": d["guid"],
            "type": "decision",
            "requirements": [],
        }

    # Junctions (merge/fork/join)
    for j in ad.get("junctions", []):
        name = j["name"]
        ctype = j.get("connector_type", "Junction").lower()
        lines.append(f'    {name}(({name}))')
        sidecar[name] = {
            "guid"          : j["guid"],
            "type"          : ctype,
            "requirements"  : [],
        }

    # Final nodes
    for f in ad.get("final_nodes", []):
        name = f["name"]
        lines.append(f'    {name}([⏹ end])')
        sidecar[name] = {
            "guid"        : f["guid"],
            "type"        : "final",
            "requirements": [],
        }

    lines.append("")  # blank line between node defs and edges

    # ── Edges ────────────────────────────────────────────────────────────
    for t in ad.get("transitions", []):
        src   = t["from"]
        tgt   = t["to"]
        guard = t.get("guard") or ""

        if src is None or tgt is None:
            continue

        if guard:
            lines.append(f'    {src} -->|{guard}| {tgt}')
        else:
            lines.append(f'    {src} --> {tgt}')

    # ── Swimlane comment (if any) ────────────────────────────────────────
    if ad.get("swimlanes"):
        lines.append("")
        lines.append("    %% Swimlanes (not rendered in Mermaid):")
        for sl in ad["swimlanes"]:
            covered = ", ".join(sl.get("covered", []))
            lines.append(f'    %% {sl["name"]}: {covered}')

    mermaid_str = "\n".join(lines)
    return mermaid_str, sidecar


def mermaid_with_context(ad: dict) -> str:
    """
    Full context block for LLM: diagram metadata + Mermaid + requirements map.
    Suitable for pasting directly into a prompt.
    """
    mermaid, sidecar = to_mermaid(ad)

    lines = [
        f"Diagram: {ad.get('diagram_name')}",
        f"Use case: {ad.get('use_case')}",
        f"Module: {ad.get('module')}",
        f"Requirements covered: {ad['summary']['total_requirements_linked']}",
        "",
        mermaid,
    ]

    # Requirements per action — separate section, clean diagram
    req_lines = []
    for name, info in sidecar.items():
        reqs = info.get("requirements", [])
        if reqs:
            req_lines.append(f"  {name}: {', '.join(reqs)}")

    if req_lines:
        lines.append("")
        lines.append("Requirements linked per action:")
        lines.extend(req_lines)

    if ad["summary"]["actions_without_requirements"]:
        lines += [
            "",
            "Actions with no linked requirements: "
            + ", ".join(ad["summary"]["actions_without_requirements"]),
        ]

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python ad_to_mermaid.py <read_detailed_ad_output.json>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        ad = json.load(f)

    mermaid, sidecar = to_mermaid(ad)
    print("=" * 60)
    print(mermaid)
    print("=" * 60)
    print(f"\nSidecar ({len(sidecar)} nodes):")
    for name, info in sidecar.items():
        reqs = info.get("requirements", [])
        print(f"  {name} [{info['type']}] guid={info['guid'][:24]} reqs={reqs}")
