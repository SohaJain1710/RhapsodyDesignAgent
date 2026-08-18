"""
mermaid_to_ad.py
-----------------
Converts a Mermaid flowchart string (LLM output) back to the JSON schema
that create_activity_diagram.py expects.

Mermaid node shapes → action types:
  name["text"]      → action
  name{text}        → decision
  name((text))      → merge  (JunctionConnector)
  name([text])      → initial or final (based on position/name)

Usage:
    from mermaid_to_ad import from_mermaid

    plan = from_mermaid(mermaid_str, diagram_name="MyAD", use_case="My use case")
    # plan is ready to pass to create_activity_diagram.py
"""
from __future__ import annotations
import re
import json


# ── Node pattern matchers ─────────────────────────────────────────────────
# Match: id["label"] or id['label']  → action
_RE_ACTION   = re.compile(r'^(\w+)\["([^"]*)"\]$')
_RE_ACTION_S = re.compile(r"^(\w+)\['([^']*)'\]$")
# Match: id{label}                   → decision
_RE_DECISION = re.compile(r'^(\w+)\{([^}]*)\}$')
# Match: id((label))                 → merge/junction
_RE_MERGE    = re.compile(r'^(\w+)\(\(([^)]*)\)\)$')
# Match: id([label])                 → initial or final
_RE_STADIUM  = re.compile(r'^(\w+)\(\[([^\]]*)\]\)$')
# Match: id(label)                   → rounded (treat as action)
_RE_ROUNDED  = re.compile(r'^(\w+)\(([^)]+)\)$')

# ── Edge pattern matchers ─────────────────────────────────────────────────
# a --> b
_RE_EDGE       = re.compile(r'^(\w+)\s*-->\s*(\w+)$')
# a -->|guard| b
_RE_EDGE_GUARD = re.compile(r'^(\w+)\s*-->\|([^|]*)\|\s*(\w+)$')
# a -- text --> b  (alternate syntax)
_RE_EDGE_TEXT  = re.compile(r'^(\w+)\s*--\s*([^-]+)\s*-->\s*(\w+)$')


def _parse_node_line(line: str) -> tuple[str, str, str] | None:
    """
    Parse a single Mermaid node definition line.
    Returns (node_id, label, node_type) or None if not a node line.
    """
    line = line.strip()
    if not line or line.startswith('%') or line.startswith('flowchart') \
            or line.startswith('graph'):
        return None

    for pattern, ntype in [
        (_RE_ACTION,   "action"),
        (_RE_ACTION_S, "action"),
        (_RE_DECISION, "decision"),
        (_RE_MERGE,    "merge"),
    ]:
        m = pattern.match(line)
        if m:
            node_id = m.group(1)
            label   = m.group(2).replace("\\n", "\n").strip()
            return node_id, label, ntype

    # Check stadium before rounded (([label]) must match before (label))
    m = _RE_STADIUM.match(line)
    if m:
        node_id = m.group(1)
        label   = m.group(2).strip()
        if any(x in label.lower() for x in ["start", "▶", "begin", "initial"]) \
                or "initial" in node_id.lower():
            return node_id, label, "initial"
        if any(x in label.lower() for x in ["end", "⏹", "stop", "final"]) \
                or any(x in node_id.lower() for x in ["final", "activityfinal", "end"]):
            return node_id, label, "final"
        return node_id, label, "final"

    m = _RE_ROUNDED.match(line)
    if m:
        node_id = m.group(1)
        label   = m.group(2).replace("\\n", "\n").strip()
        return node_id, label, "action"

    return None


def _parse_edge_line(line: str) -> tuple[str, str, str] | None:
    """
    Parse a single Mermaid edge line.
    Returns (from_id, to_id, guard) or None.
    """
    line = line.strip()
    m = _RE_EDGE_GUARD.match(line)
    if m:
        return m.group(1), m.group(3), m.group(2).strip()
    m = _RE_EDGE_TEXT.match(line)
    if m:
        return m.group(1), m.group(3), m.group(2).strip()
    m = _RE_EDGE.match(line)
    if m:
        return m.group(1), m.group(2), ""
    return None


def from_mermaid(
    mermaid_str: str,
    diagram_name: str = "NewDiagram",
    use_case: str = "",
    requirements_map: dict | None = None,
) -> dict:
    """
    Convert a Mermaid flowchart string to create_activity_diagram.py JSON.

    Args:
        mermaid_str:      Mermaid flowchart string from LLM output
        diagram_name:     Name to assign the diagram in Rhapsody
        use_case:         Use case name (metadata only)
        requirements_map: Optional {node_id: [req_ids]} to attach requirements
                          to actions. Comes from the "Requirements linked per
                          action" section that the LLM may have modified.

    Returns:
        dict matching create_activity_diagram.py's plan schema
    """
    requirements_map = requirements_map or {}

    nodes   = {}   # id -> {label, type}
    edges   = []   # [{from, to, guard}]
    id_ctr  = [0]

    def next_id():
        id_ctr[0] += 1
        return f"n{id_ctr[0]}"

    # ── Pre-process: join multiline node labels ──────────────────────────
    # Mermaid node labels can span multiple lines when the label contains \n
    # as actual newlines. We join continuation lines (those that don't start
    # a new node or edge) back onto the previous node definition line.
    processed_lines = []
    for raw_line in mermaid_str.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            processed_lines.append("")
            continue
        # If this looks like a continuation of a node label (no --> and no
        # leading word+bracket pattern) and previous line has an open quote
        if (processed_lines and
                '"""' not in stripped and
                '-->' not in stripped and
                not re.match(r'^\w+[\[\{(\(]', stripped) and
                not stripped.startswith('%') and
                not stripped.startswith('flowchart') and
                not stripped.startswith('graph') and
                processed_lines[-1].count('"') % 2 == 1):
            processed_lines[-1] = processed_lines[-1] + "\\n" + stripped
        else:
            processed_lines.append(raw_line)

    # ── Parse lines ───────────────────────────────────────────────────────
    for raw_line in processed_lines:
        line = raw_line.strip()

        # Skip comments and directives — but parse AllocatedToModule comments
        if not line or line.startswith('flowchart') or line.startswith('graph'):
            continue
        if line.startswith('%%') or line.startswith('%'):
            # Parse: %% node_id AllocatedToModule: module_name
            import re as _re
            m = _re.match(r'%%?\s*(\w+)\s+AllocatedToModule:\s*(.+)', line)
            if m:
                nid, module = m.group(1).strip(), m.group(2).strip()
                if nid in nodes:
                    nodes[nid]["allocated_module"] = module
                else:
                    # Node not yet registered — store for later merge
                    nodes.setdefault(nid, {})["allocated_module"] = module
            continue

        # Try edge first (edges contain --> which can look like nodes)
        edge = _parse_edge_line(line)
        if edge:
            src, tgt, guard = edge
            edges.append({"from": src, "to": tgt, "guard": guard})
            # Register bare node ids if not yet seen
            for nid in (src, tgt):
                if nid not in nodes:
                    if "initial" in nid.lower() or nid == "initial":
                        nodes[nid] = {"label": "start", "type": "initial"}
                    elif any(x in nid.lower() for x in ["final", "activityfinal", "end"]):
                        nodes[nid] = {"label": "end", "type": "final"}
                    else:
                        nodes[nid] = {"label": nid, "type": "action"}
            continue

        node = _parse_node_line(line)
        if node:
            nid, label, ntype = node
            nodes[nid] = {"label": label, "type": ntype}

    # ── Post-parse: reclassify misidentified final/initial nodes ─────────────
    for nid in list(nodes.keys()):
        if nodes[nid]["type"] == "action":
            if any(x in nid.lower() for x in ["activityfinal", "flowfinal",
                                               "final", "end_"]):
                nodes[nid]["type"] = "final"

    # ── Build actions list ────────────────────────────────────────────────
    actions    = []
    id_counter = 0

    for nid, info in nodes.items():
        ntype = info["type"]
        if ntype in ("initial", "final"):
            continue  # handled as sentinels in transitions
        # Also skip if name looks like final
        if nid.lower() in ("initial", "final") or "final" in nid.lower():
            continue

        id_counter += 1
        action_id = f"a{id_counter}" if not nid[0].isdigit() else f"a_{nid}"

        reqs = requirements_map.get(nid, [])
        actions.append({
            "id"             : nid,
            "name"           : nid,
            "type"           : ntype,
            "text"           : info["label"],
            "requirements"   : reqs,
            "allocated_module": info.get("allocated_module", ""),
        })

    # ── Build transitions list ────────────────────────────────────────────
    # Normalise initial/final sentinels
    initial_ids = {nid for nid, info in nodes.items() if info["type"] == "initial"}
    final_ids   = {nid for nid, info in nodes.items() if info["type"] == "final"}

    transitions = []
    for e in edges:
        src = "initial" if e["from"] in initial_ids else e["from"]
        tgt = "final"   if e["to"]   in final_ids   else e["to"]
        transitions.append({
            "from" : src,
            "to"   : tgt,
            "guard": e["guard"],
        })

    return {
        "diagram_name": diagram_name,
        "use_case"    : use_case,
        "actions"     : actions,
        "transitions" : transitions,
        "swimlanes"   : [],
    }


def parse_requirements_section(context_block: str) -> dict:
    """
    Parse the 'Requirements linked per action:' section from mermaid_with_context()
    output (or LLM-modified version).

    Returns dict: node_id -> [req_ids]
    """
    result = {}
    in_section = False
    for line in context_block.splitlines():
        if "Requirements linked per action:" in line:
            in_section = True
            continue
        if in_section:
            if not line.strip() or (line.strip() and not line.strip().startswith("-")
                                     and ":" not in line):
                break
            # Format: "  action_0: SRS_SDM_200, SRS_SDM_198"
            m = re.match(r'\s+(\w+):\s*(.+)', line)
            if m:
                nid  = m.group(1)
                reqs = [r.strip() for r in m.group(2).split(",") if r.strip()]
                result[nid] = reqs
    return result


if __name__ == "__main__":
    import sys

    # Quick test with a sample Mermaid string
    sample = """flowchart TD
    initial([▶ start])
    action_0["1. Check NVM section is configured
2. Check if Target Data Set is in RAM"]
    action_12["Load data from ROM to RAM"]
    action_19["Load data from NVM to RAM"]
    decision{decision}
    mergenode_98((mergenode_98))
    activityfinal_30([⏹ end])

    initial --> action_0
    action_0 --> decision
    decision -->|no| action_12
    decision -->|yes| action_19
    action_12 --> mergenode_98
    action_19 --> mergenode_98
    mergenode_98 --> activityfinal_30
"""

    req_map = {
        "action_0"  : ["SRS_SDM_200", "SRS_SDM_198"],
        "action_12" : ["SRS_SDM_203", "SRS_SDM_205"],
        "action_19" : ["SRS_SDM_183", "SRS_SDM_195"],
    }

    plan = from_mermaid(sample, diagram_name="LoadSdmDataAD",
                        use_case="Load sdm data", requirements_map=req_map)
    print(json.dumps(plan, indent=2))
