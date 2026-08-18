"""
mermaid_to_ibd.py
-----------------
Converts ibd_to_mermaid.py output back to create_ibd.py plan JSON.

Parses the graph LR Mermaid format:
  - Subgraphs → objects with their ports
  - Node labels → port name + provided/required interfaces
  - Edges → links between objects via ports

Usage:
    from mermaid_to_ibd import from_mermaid
    plan = from_mermaid(mermaid_str, component_name="rb_sdm_SafeDataMgt")
"""
from __future__ import annotations
import re
import json


def from_mermaid(mermaid_str: str, component_name: str = "") -> dict:
    """
    Parse Mermaid graph LR string back to create_ibd.py plan JSON.

    Returns:
    {
        "component_name": "...",
        "ports": [{"name": "...", "provided": [...], "required": [...]}],
        "links": [{"from_port": "...", "from_obj": "...",
                   "to_port": "...", "to_obj": "..."}]
    }
    """
    ports = []
    links = []
    port_to_obj = {}   # port_name -> obj_name (which subgraph it belongs to)

    current_subgraph = None
    lines = mermaid_str.splitlines()

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("%%") or line.startswith("graph"):
            continue

        # ── Subgraph start: subgraph obj_name ["label"]
        m = re.match(r'subgraph\s+(\w+)\s*\[?"?([^"\]]*)"?\]?', line)
        if m:
            current_subgraph = m.group(1)
            continue

        if line == "end":
            current_subgraph = None
            continue

        # ── Port node inside subgraph: obj__port["port_name\n+iface1, iface2"]
        # Node IDs use __ separator: {obj_name}__{port_name}
        m = re.match(r'(\w+)\["([^"]+)"\]', line)
        if m and current_subgraph:
            node_id = m.group(1)
            label   = m.group(2).replace("\\n", "\n")
            parts_  = label.split("\n")
            port_name = parts_[0].strip()
            provided = []
            required = []
            if len(parts_) > 1:
                iface_str = parts_[1].strip()
                if iface_str.startswith("+"):
                    ifaces = [x.strip() for x in iface_str[1:].split(",")]
                    provided = [x for x in ifaces if x and x != "..."]
                elif iface_str.startswith("-"):
                    ifaces = [x.strip() for x in iface_str[1:].split(",")]
                    required = [x for x in ifaces if x and x != "..."]

            ports.append({
                "name"    : port_name,
                "provided": provided,
                "required": required,
            })
            # Map both node_id and port_name to the current subgraph
            port_to_obj[node_id]   = (current_subgraph, port_name)
            port_to_obj[port_name] = (current_subgraph, port_name)
            continue

        # ── Edge: src -->|label| tgt or src --> tgt
        m = re.match(r'(\w+)\s*-->\|([^|]*)\|\s*(\w+)', line)
        if not m:
            m = re.match(r'(\w+)\s*-->\s*(\w+)', line)
            if m:
                src, tgt, label = m.group(1), m.group(2), ""
            else:
                continue
        else:
            src, tgt, label = m.group(1), m.group(3), m.group(2)

        # Resolve which objects own these ports
        # Node IDs are either "obj__port" or plain port_name
        def resolve(node_id):
            if "__" in node_id:
                parts_ = node_id.split("__", 1)
                return parts_[0], parts_[1]  # obj_name, port_name
            info = port_to_obj.get(node_id)
            if info:
                return info[0], info[1]
            return None, node_id

        from_obj, from_port_resolved = resolve(src)
        to_obj,   to_port_resolved   = resolve(tgt)

        if from_obj and to_obj and from_obj != to_obj:
            links.append({
                "from_port": from_port_resolved,
                "from_obj" : from_obj,
                "to_port"  : to_port_resolved,
                "to_obj"   : to_obj,
            })

    # Deduplicate ports — same port may appear in multiple subgraphs
    seen_ports = {}
    for port in ports:
        name = port["name"]
        if name not in seen_ports:
            seen_ports[name] = port
        else:
            # Merge interfaces
            seen_ports[name]["provided"] = list(set(
                seen_ports[name]["provided"] + port["provided"]))
            seen_ports[name]["required"] = list(set(
                seen_ports[name]["required"] + port["required"]))

    return {
        "component_name": component_name,
        "ports"         : list(seen_ports.values()),
        "links"         : links,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python mermaid_to_ibd.py <mermaid_file.mmd> [component_name]")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        mermaid_str = f.read()
    component = sys.argv[2] if len(sys.argv) > 2 else ""
    plan = from_mermaid(mermaid_str, component)
    print(json.dumps(plan, indent=2))
