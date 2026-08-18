"""
bdd_to_mermaid.py
-----------------
Converts read_bdd.py output to Mermaid classDiagram format.

Usage:
    from bdd_to_mermaid import to_mermaid, mermaid_with_context
    mermaid_str = to_mermaid(bdd_json)
"""
from __future__ import annotations


def _visibility(v: str) -> str:
    return {"public": "+", "protected": "#", "private": "-"}.get(
        str(v).lower(), "+")


def _op_signature(op: dict) -> str:
    args = ", ".join(
        f"{a['name']}: {a['type']}" if a.get('type') else a['name']
        for a in op.get("arguments", [])
    )
    ret = op.get("return_type", "void") or "void"
    vis = _visibility(op.get("visibility", "public"))
    stereos = op.get("stereotypes", [])
    stereo_str = f"«{','.join(stereos)}» " if stereos else ""
    return f"{vis}{stereo_str}{op['name']}({args}) {ret}"


def _attr_line(attr: dict) -> str:
    vis = _visibility(attr.get("visibility", "private"))
    typ = attr.get("type", "") or ""
    stereos = attr.get("stereotypes", [])
    stereo_str = f"«{','.join(stereos)}» " if stereos else ""
    dv = attr.get("default_value", "") or ""
    # Strip default values with braces/parens — Mermaid parser can't handle them
    if any(c in dv for c in "{}()[]"):
        dv = ""
    default = f" = {dv}" if dv else ""
    return f"{vis}{stereo_str}{attr['name']}: {typ}{default}"


def to_mermaid(bdd: dict) -> str:
    """Convert a single BDD dict to Mermaid classDiagram string."""
    lines = ["classDiagram"]

    # Build a name map to handle duplicate class names
    name_count = {}
    for cls in bdd.get("classes", []):
        name_count[cls["name"]] = name_count.get(cls["name"], 0) + 1

    def display_name(cls):
        if name_count.get(cls["name"], 1) > 1:
            role = cls.get("role", "class")
            if role == "component":
                return f"{cls['name']}_SWComponent"
            elif role == "module":
                return f"{cls['name']}_Module"
            else:
                return f"{cls['name']}_{role.capitalize()}"
        return cls["name"]

    # Classes
    for cls in bdd.get("classes", []):
        cls_name = display_name(cls)
        stereos  = cls.get("stereotypes", [])
        ops      = cls.get("operations", [])
        attrs    = cls.get("attributes", [])

        lines.append(f"    class {cls_name} {{")

        if stereos:
            lines.append(f"        <<{','.join(stereos)}>>")

        for attr in attrs:
            lines.append(f"        {_attr_line(attr)}")

        for op in ops:
            lines.append(f"        {_op_signature(op)}")

        lines.append("    }")
        lines.append("")

    # Generalizations / Realizations
    for gen in bdd.get("generalizations", []):
        specific = gen.get("specific", "")
        general  = gen.get("general", "")
        rel_type = gen.get("type", "Generalization")
        if specific and general:
            if rel_type == "Realization":
                lines.append(f"    {specific} ..|> {general} : realizes")
            else:
                lines.append(f"    {specific} --|> {general}")

    # Dependencies
    for dep in bdd.get("dependencies", []):
        from_cls = dep.get("from", "")
        to_cls   = dep.get("to", "")
        stereos  = dep.get("stereotypes", [])
        if from_cls and to_cls:
            label = f" : {','.join(stereos)}" if stereos else ""
            lines.append(f"    {from_cls} ..> {to_cls}{label}")

    # Composition — component class owns part instances
    class_by_name = {c["name"]: c for c in bdd.get("classes", [])}
    objects = bdd.get("objects", [])

    comp_class = next(
        (c for c in bdd.get("classes", []) if c.get("role") == "component"),
        None
    )
    if comp_class and objects:
        owner = display_name(comp_class)
        for obj in objects:
            obj_type = obj.get("type")
            obj_name = obj.get("name")
            if obj_type and obj_type in class_by_name:
                tgt = display_name(class_by_name[obj_type])
                # Show: owner *-- tgt : instanceName
                lines.append(f'    {owner} *-- {tgt} : {obj_name}')

    return "\n".join(lines)


def mermaid_with_context(bdd: dict, unrealized: dict = None) -> str:
    """Full context block for LLM."""
    s = bdd.get("summary", {})
    mermaid = to_mermaid(bdd)
    lines = mermaid.splitlines()
    meta = [
        f"    %% Diagram: {bdd.get('diagram_name')}",
        f"    %% Classes: {s.get('total_classes',0)} | "
        f"Operations: {s.get('total_operations',0)} | "
        f"Attributes: {s.get('total_attributes',0)}",
    ]
    # Add unrealized interfaces as comments for LLM context
    if unrealized and unrealized.get("unrealized"):
        meta.append(f"    %% UNREALIZED INTERFACES (LLM: consider realizing these):")
        for intf in unrealized["unrealized"]:
            meta.append(f"    %%   - {intf}")
    for i, line in enumerate(lines):
        if line.strip() == "classDiagram":
            lines = lines[:i+1] + meta + lines[i+1:]
            break
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python bdd_to_mermaid.py <read_bdd_output.json>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    for bdd in data.get("bdds", []):
        print(mermaid_with_context(bdd))
        print()
