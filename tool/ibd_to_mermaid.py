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


def to_mermaid(ibd: dict) -> str:
    diagram_name = ibd.get("diagram_name", "IBD")
    objects      = ibd.get("objects", [])
    ports        = {p["name"]: p for p in ibd.get("ports", [])}
    links        = ibd.get("links", [])

    lines = [
        f"graph LR",
        f"",
        f"    %% IBD: {diagram_name}",
        f"",
    ]

    # Build a map: object_name -> [port_names]
    obj_ports = {o["name"]: o.get("ports", []) for o in objects}

    # Draw each object as a subgraph with its ports
    for obj in objects:
        obj_name = obj["name"]
        obj_id   = obj_name.replace("-", "_")
        mc       = obj.get("metaclass", "Object")
        # Class = component boundary, Object = internal part instance
        if mc == "Class":
            label = f"«Component»\\n{obj_name}"
        else:
            label = f"«Part»\\n{obj_name}"

        lines.append(f"    subgraph {obj_id} [\"{label}\"]")
        for port_name in obj.get("ports", []):
            port_id = f"{obj_id}__{port_name.replace('-','_')}"
            port    = ports.get(port_name, {})
            prov    = port.get("provided", [])
            req     = port.get("required", [])
            if prov:
                iface_str = ", ".join(prov[:2]) + ("..." if len(prov) > 2 else "")
                lines.append(f'        {port_id}["{port_name}\\n+{iface_str}"]')
            elif req:
                iface_str = ", ".join(req[:2]) + ("..." if len(req) > 2 else "")
                lines.append(f'        {port_id}["{port_name}\\n-{iface_str}"]')
            else:
                lines.append(f'        {port_id}["{port_name}"]')
        lines.append(f"    end")
        lines.append(f"")

    # Draw links as edges between port nodes
    lines.append(f"    %% Connections")
    for link in links:
        from_port = link.get("from_port")
        from_obj  = link.get("from_obj")
        to_port   = link.get("to_port")
        to_obj    = link.get("to_obj")

        # Swap reversed endpoints (some links have only toPort set)
        if not from_port and not from_obj and (to_port or to_obj):
            from_port, to_port = to_port, from_port
            from_obj,  to_obj  = to_obj,  from_obj

        if not (from_port or from_obj):
            continue  # skip fully unresolved

        # Build source node id
        if from_port and from_obj:
            src = f"{from_obj.replace('-','_')}__{from_port.replace('-','_')}"
        elif from_obj:
            src = from_obj.replace("-", "_")
        else:
            src = from_port.replace("-", "_") if from_port else None

        # Build target node id — even partial (obj only) is useful
        if to_port and to_obj:
            tgt = f"{to_obj.replace('-','_')}__{to_port.replace('-','_')}"
        elif to_obj:
            tgt = to_obj.replace("-", "_")
        elif to_port:
            for o in objects:
                if to_port in o.get("ports", []):
                    tgt = f"{o['name'].replace('-','_')}__{to_port.replace('-','_')}"
                    break
            else:
                tgt = to_port.replace("-", "_")
        elif from_obj:
            # Only from side known — still render as comment
            lines.append(f"    %% {link.get('name','?')}: {src} -> (unresolved)")
            continue
        else:
            continue

        if src and tgt:
            # Skip self-loops (same port connected to itself)
            if src == tgt:
                lines.append(f"    %% skipped self-loop: {src}")
                continue
            # Use port names as label — more readable than truncated link name
            if from_port and to_port and from_port != to_port:
                label = f"{from_port[:20]}↔{to_port[:20]}"
                lines.append(f"    {src} -->|{label}| {tgt}")
            else:
                lines.append(f"    {src} --> {tgt}")

    return "\n".join(lines)


def mermaid_with_context(ibd: dict) -> str:
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
