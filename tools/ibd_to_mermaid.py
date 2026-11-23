"""
ibd_to_mermaid.py
-----------------
Converts read_ibd.py output (package-level IBD) to a Mermaid graph diagram.

Shows:
  - Objects (part instances) as subgraphs
  - Ports on each object
  - Links (connectors) between ports as edges
"""
from __future__ import annotations


import re as _re


def _id(*parts) -> str:
    return "__".join(_re.sub(r"\W", "_", str(p or "")) for p in parts)


def _label(text: str) -> str:
    return str(text).replace('"', "'")


def to_mermaid(ibd: dict) -> str:
    """IBD as Mermaid: objects as subgraphs, ports with FULL contracts
    (+provided / -required), links between ports."""
    if not ibd:
        return "graph LR\n    %% No IBD (StructureDiagram) in the DetailedDesign package"
    objects = ibd.get("objects", [])
    port_by = {(p.get("owner"), p["name"]): p for p in ibd.get("ports", [])}
    lines = ["graph LR", "", f"    %% IBD: {ibd.get('diagram_name', 'IBD')}", ""]

    for obj in objects:
        name = obj["name"]
        mc = obj.get("metaClass") or obj.get("metaclass") or "Object"
        typ = obj.get("type") or name
        label = (f"«Component» {name}" if mc == "Class"
                 else f"«Part» {name} : {typ}")
        lines.append(f'    subgraph {_id(name)} ["{_label(label)}"]')
        for pn in obj.get("ports", []):
            p = port_by.get((name, pn), {})
            contract = " ".join([f"+{i}" for i in p.get("provided", [])] +
                                [f"-{i}" for i in p.get("required", [])])
            text = f"{pn}<br>{contract}" if contract else pn
            lines.append(f'        {_id(name, pn)}["{_label(text)}"]')
        lines.append("    end")
        lines.append("")

    lines.append("    %% Connections (obj.port -- obj.port)")
    for l in ibd.get("links", []):
        fo, fp, to, tp = l.get("from_obj"), l.get("from_port"), l.get("to_obj"), l.get("to_port")
        if not (fp and tp):
            lines.append(f"    %% {l.get('name') or 'link'}: {fo}.{fp} -- {to}.{tp} (incomplete)")
            continue
        src, tgt = _id(fo, fp), _id(to, tp)
        if src != tgt:
            lines.append(f"    {src} --- {tgt}")
    return "\n".join(lines)


def mermaid_with_context(ibd: dict) -> str:
    if not ibd:
        return to_mermaid(ibd)
    s = ibd.get("summary", {})
    mermaid = to_mermaid(ibd)
    # Insert metadata as comments at the top of the diagram
    # (after the graph LR declaration so Mermaid.js doesn't choke)
    meta_comments = [
        f"    %% Diagram: {ibd.get('diagram_name')}",
        f"    %% Objects: {s.get('total_objects', 0)} | Ports: {s.get('total_ports', 0)} | Links: {s.get('total_links', 0)}",
    ]
    lines = mermaid.splitlines()
    # Insert after "graph LR"
    for i, line in enumerate(lines):
        if line.strip().startswith("graph"):
            lines = lines[:i+1] + meta_comments + lines[i+1:]
            break
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python ibd_to_mermaid.py <ibd_output.json>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    print(mermaid_with_context(data))
