"""
mermaid_to_bdd.py
-----------------
Parses Mermaid classDiagram back to create_bdd.py plan JSON.

Input Mermaid format:
    classDiagram
        class rb_sdm_SafeDataMgt {
            <<AB12Module>>
            +rb_sdm_Init() void
            -attr: uint8
        }
        rb_sdm_SafeDataMgt ..|> rb_sdm_InfrastructurIntf : realizes

Output plan:
{
    "component_name": "rb_sdm_SafeDataMgt",
    "classes": [
        {
            "name": "rb_sdm_SafeDataMgt",
            "stereotypes": ["AB12Module"],
            "operations": [{"name": "rb_sdm_Init", "return_type": "void", ...}],
            "attributes": [{"name": "attr", "type": "uint8", ...}]
        }
    ],
    "realizations": [
        {"specific": "rb_sdm_SafeDataMgt", "general": "rb_sdm_InfrastructurIntf"}
    ],
    "generalizations": [
        {"specific": "rb_sdm_ProvideConfigDataIntf", "general": "rb_sdm_GeneralIntf"}
    ]
}
"""
from __future__ import annotations
import re
import json


def _parse_visibility(line: str) -> tuple[str, str]:
    """Return (visibility, rest_of_line)."""
    if line.startswith("+"):
        return "public", line[1:]
    elif line.startswith("-"):
        return "private", line[1:]
    elif line.startswith("#"):
        return "protected", line[1:]
    return "public", line


def _parse_stereotypes(text: str) -> list[str]:
    """Extract stereotypes from <<A,B>> pattern."""
    m = re.match(r"«([^»]+)»", text)
    if m:
        return [s.strip() for s in m.group(1).split(",")]
    return []


def _parse_operation(line: str) -> dict | None:
    """Parse: +opName(arg: Type, ...) ReturnType"""
    vis, rest = _parse_visibility(line.strip())
    # Strip stereotype prefix «...»
    stereos = _parse_stereotypes(rest)
    rest = re.sub(r"«[^»]+»\s*", "", rest)

    m = re.match(r"(\w+)\(([^)]*)\)\s*(\S*)", rest.strip())
    if not m:
        return None
    name       = m.group(1)
    args_str   = m.group(2).strip()
    return_type = m.group(3).strip() or "void"

    arguments = []
    if args_str:
        for arg in args_str.split(","):
            arg = arg.strip()
            if ":" in arg:
                parts = arg.split(":", 1)
                arguments.append({"name": parts[0].strip(), "type": parts[1].strip()})
            elif arg:
                arguments.append({"name": arg, "type": ""})

    return {
        "name"       : name,
        "return_type": return_type,
        "arguments"  : arguments,
        "stereotypes": stereos,
        "visibility" : vis,
    }


def _parse_attribute(line: str) -> dict | None:
    """Parse: -attrName: Type = defaultValue"""
    vis, rest = _parse_visibility(line.strip())
    stereos = _parse_stereotypes(rest)
    rest = re.sub(r"«[^»]+»\s*", "", rest)

    # Skip if it looks like an operation (has parentheses)
    if "(" in rest:
        return None

    default_value = ""
    if "=" in rest:
        parts = rest.split("=", 1)
        rest  = parts[0].strip()
        default_value = parts[1].strip()

    if ":" in rest:
        parts = rest.split(":", 1)
        name  = parts[0].strip()
        typ   = parts[1].strip()
    else:
        name = rest.strip()
        typ  = ""

    if not name:
        return None

    return {
        "name"         : name,
        "type"         : typ,
        "default_value": default_value,
        "stereotypes"  : stereos,
        "visibility"   : vis,
    }


DISPLAY_SUFFIXES = ["_SWComponent", "_Module", "_Config", "_Component",
                   "_Capitalize", "_Class"]


def _strip_suffix(name: str) -> str:
    """Strip display suffixes added by bdd_to_mermaid.py."""
    for suffix in DISPLAY_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def from_mermaid(mermaid_str: str, component_name: str = "") -> dict:
    """Parse Mermaid classDiagram to plan JSON."""
    classes        = []
    realizations   = []
    generalizations = []

    current_class  = None
    in_class_body  = False

    for raw in mermaid_str.splitlines():
        line = raw.strip()

        if not line or line.startswith("%%") or line == "classDiagram":
            continue

        # Class declaration: class Name {
        m = re.match(r"class\s+(\w+)\s*\{?", line)
        if m:
            raw_name = m.group(1)
            real_name = _strip_suffix(raw_name)
            current_class = {
                "name"       : real_name,
                "stereotypes": [],
                "operations" : [],
                "attributes" : [],
            }
            classes.append(current_class)
            in_class_body = "{" in line
            continue

        if line == "}":
            in_class_body = False
            current_class = None
            continue

        if in_class_body and current_class is not None:
            if line == "{":
                continue
            # Stereotype: <<A,B>>
            m = re.match(r"<<([^>]+)>>", line)
            if m:
                current_class["stereotypes"] = [
                    s.strip() for s in m.group(1).split(",")]
                continue

            # Try operation first
            if "(" in line:
                op = _parse_operation(line)
                if op:
                    current_class["operations"].append(op)
                continue

            # Try attribute
            if line.startswith(("+", "-", "#")) or ":" in line:
                attr = _parse_attribute(line)
                if attr:
                    current_class["attributes"].append(attr)
                continue

        # Realization: A ..|> B : realizes
        m = re.match(r"(\w+)\s*\.\.\|>\s*(\w+)", line)
        if m:
            realizations.append({
                "specific": _strip_suffix(m.group(1)),
                "general" : _strip_suffix(m.group(2)),
            })
            continue

        # Generalization: A --|> B
        m = re.match(r"(\w+)\s*--\|>\s*(\w+)", line)
        if m:
            generalizations.append({
                "specific": _strip_suffix(m.group(1)),
                "general" : _strip_suffix(m.group(2)),
            })
            continue

        # Composition: A *-- "label" B
        # (read-only for context — not applied in create_bdd)

    # Deduplicate classes — after stripping suffixes, same name may appear twice
    seen = {}
    for cls in classes:
        name = cls["name"]
        if name not in seen:
            seen[name] = cls
        else:
            # Merge: keep the one with more operations/attributes
            existing = seen[name]
            if (len(cls["operations"]) + len(cls["attributes"]) >
                    len(existing["operations"]) + len(existing["attributes"])):
                seen[name] = cls

    return {
        "component_name" : component_name,
        "classes"        : list(seen.values()),
        "realizations"   : realizations,
        "generalizations": generalizations,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python mermaid_to_bdd.py <mermaid.mmd> [component_name]")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        mermaid_str = f.read()
    component = sys.argv[2] if len(sys.argv) > 2 else ""
    plan = from_mermaid(mermaid_str, component)
    print(json.dumps(plan, indent=2))
