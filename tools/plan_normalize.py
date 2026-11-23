"""
plan_normalize.py
-----------------
LLM output is not always in the exact JSON shape the Rhapsody tools expect.
For example, interface operations sometimes come back as strings such as
    "rb_sdm_GetData(uint8 idx): uint8"
instead of
    {"name": "rb_sdm_GetData", "arguments": [{"name": "idx", "type": "uint8"}],
     "return_type": "uint8"}
These helpers turn every accepted variant into the dict form. Pure Python,
no COM, safe to import anywhere.
"""
from __future__ import annotations
import re

_VIS = {"+": "public", "-": "private", "#": "protected"}
_SIG = re.compile(
    r"^(?:(?P<pre>[\w:<>\*\s]+?)\s+)?"          # optional leading return type (C style)
    r"(?P<name>\w+)\s*\((?P<args>[^)]*)\)\s*"   # name(args)
    r"(?::|->)?\s*(?P<post>[\w:<>\*\s]*)$"      # optional trailing return type
)


def _norm_arg(a) -> dict | None:
    if isinstance(a, dict):
        if not a.get("name"):
            return None
        return {**a, "type": a.get("type", "") or ""}
    text = str(a).strip()
    if not text or text.lower() == "void":
        return None
    if ":" in text:                                   # "name: type"
        n, t = text.split(":", 1)
        return {"name": n.strip(), "type": t.strip()}
    parts = text.replace("*", " * ").split()
    if len(parts) >= 2:                               # "type name"  (C style)
        return {"name": parts[-1], "type": re.sub(r"\s*\*", "*", " ".join(parts[:-1])).strip()}
    return {"name": text, "type": ""}


def normalize_operation(op) -> dict | None:
    """dict or signature string → operation dict (None if unusable)."""
    if isinstance(op, dict):
        if not op.get("name"):
            return None
        out = dict(op)
        out["arguments"] = [x for x in (_norm_arg(a) for a in op.get("arguments") or []) if x]
        out["return_type"] = op.get("return_type") or "void"
        out["visibility"] = op.get("visibility") or "public"
        return out

    text = str(op or "").strip()
    if not text:
        return None
    vis = None
    if text[0] in _VIS:
        vis, text = _VIS[text[0]], text[1:].strip()

    m = _SIG.match(text)
    if m:
        args = [x for x in (_norm_arg(a) for a in m.group("args").split(",")) if x]
        ret = (m.group("post") or "").strip() or (m.group("pre") or "").strip() or "void"
        out = {"name": m.group("name"), "arguments": args, "return_type": ret}
    else:
        name = re.match(r"\w+", text)
        if not name:
            return None
        out = {"name": name.group(0), "arguments": [], "return_type": "void"}
    out["visibility"] = vis or "public"
    return out


def normalize_attribute(attr) -> dict | None:
    if isinstance(attr, dict):
        return dict(attr) if attr.get("name") else None
    text = str(attr or "").strip()
    vis = None
    if text and text[0] in _VIS:
        vis, text = _VIS[text[0]], text[1:].strip()
    a = _norm_arg(text)
    if a and vis:
        a["visibility"] = vis
    return a


def normalize_class(cls) -> dict | None:
    """Interface/class dict (or bare name string) → class dict with normalized members."""
    if isinstance(cls, str):
        cls = {"name": cls.strip()}
    if not isinstance(cls, dict) or not cls.get("name"):
        return None
    out = dict(cls)
    out["operations"] = [x for x in (normalize_operation(o) for o in cls.get("operations") or []) if x]
    out["attributes"] = [x for x in (normalize_attribute(a) for a in cls.get("attributes") or []) if x]
    st = cls.get("stereotypes") or []
    out["stereotypes"] = [st] if isinstance(st, str) else list(st)
    return out


def normalize_operations(ops) -> list:
    return [x for x in (normalize_operation(o) for o in ops or []) if x]


def normalize_classes(classes) -> list:
    return [x for x in (normalize_class(c) for c in classes or []) if x]


# ── Realizer / module validation ──────────────────────────────────────────────
# In AB12 an interface is realized by a MODULE class (stereotype AB12Module) and
# new operations belong to a module. The LLM sometimes names the use case, the
# SW component or a display alias (…_Module / …_SWComponent) instead.

_ALIAS_SUFFIXES = ("_SWComponent", "_Module", "_Component")


def _strip_alias(name: str) -> str:
    for suf in _ALIAS_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def resolve_module(name, module_names: list, component_name: str = "") -> str | None:
    """Map an LLM-provided class name to a known module class name (or None)."""
    if not name or not isinstance(name, str):
        return None
    cand = name.strip()
    for c in (cand, _strip_alias(cand)):
        if c in module_names:
            return c
        low = {m.lower(): m for m in module_names}
        if c.lower() in low:
            return low[c.lower()]
    # No module list known (e.g. context could not be read): only accept the
    # component name itself, which is the legacy behaviour (module == component name)
    if not module_names and component_name and _strip_alias(cand) == component_name:
        return component_name
    return None


def default_module(module_names: list, component_name: str = "") -> str | None:
    if len(module_names) == 1:
        return module_names[0]
    if not module_names:
        return component_name or None
    return None


def validate_design(new_ops: list, new_intfs: list, realizations: list,
                    module_names: list, component_name: str = "") -> dict:
    """Make every operation owner and every realizer a known module class.

    Returns {"new_operations", "new_interfaces", "realizations", "warnings"}.
    """
    module_names = list(module_names or [])
    fallback = default_module(module_names, component_name)
    warnings = []

    ops = []
    for op in normalize_operations(new_ops):
        given = op.get("module") or ""
        mod = resolve_module(given, module_names, component_name) or fallback
        if mod is None:
            mod = module_names[0]
            warnings.append(f"Operation {op['name']}: module '{given or '?'}' is not a module "
                            f"of this component - assigned to {mod}, please check.")
        elif given and resolve_module(given, module_names, component_name) is None:
            warnings.append(f"Operation {op['name']}: '{given}' is not a module - assigned to {mod}.")
        op["module"] = mod
        ops.append(op)

    intfs = normalize_classes(new_intfs)
    intf_names = {i["name"] for i in intfs}

    reals, seen = [], set()

    def add(mod, general):
        if (mod, general) not in seen:
            seen.add((mod, general))
            reals.append({"specific": mod, "general": general})

    llm_reals = [r for r in (realizations or [])
                 if isinstance(r, dict) and r.get("general")]

    # New interfaces: realized_by → matching "realizations" entry → single module
    for intf in intfs:
        name = intf["name"]
        given = [intf.get("realized_by", "")] + [r.get("specific", "") for r in llm_reals
                                                 if str(r["general"]).strip() == name]
        given = list(dict.fromkeys(g for g in given if g))
        mod = next((m for m in (resolve_module(g, module_names, component_name) for g in given) if m), None)
        if mod is None:
            mod = fallback
            if mod is None:
                warnings.append(f"Interface {name}: realizer {given or '(none)'} is not a module "
                                f"of this component - realization dropped, please set 'Realized By'.")
            elif given:
                warnings.append(f"Interface {name}: '{given[0]}' is not a module - "
                                f"realized by {mod} instead.")
        intf["realized_by"] = mod or ""
        if mod:
            add(mod, name)

    # Realizations of EXISTING interfaces (e.g. previously unrealized ones)
    for r in llm_reals:
        general = str(r["general"]).strip()
        if general in intf_names:
            continue
        mod = resolve_module(r.get("specific", ""), module_names, component_name) or fallback
        if mod is None:
            warnings.append(f"Realization of {general}: '{r.get('specific', '')}' is not a module - dropped.")
            continue
        add(mod, general)

    return {"new_operations": ops, "new_interfaces": intfs,
            "realizations": reals, "warnings": warnings}


def build_bdd_plans(component_name: str, new_ops: list, new_intfs: list,
                    realizations: list, interface_stereotypes: list = None,
                    interface_pkg: str = "") -> tuple[list, dict]:
    """Plans for create_bdd_from_plan: one per module (operations) + one for interfaces."""
    by_module: dict = {}
    for op in new_ops or []:
        mod = op.get("module") or component_name
        clean = {k: v for k, v in op.items() if k not in ("module", "usecase", "req_ids", "rationale")}
        by_module.setdefault(mod, []).append(clean)
    op_plans = [{
        "component_name": component_name,
        "create_missing": False,          # never invent a module class
        "classes"       : [{"name": mod, "stereotypes": [], "operations": ops, "attributes": []}],
        "realizations"  : [],
        "generalizations": [],
    } for mod, ops in by_module.items()]

    intf_plan = {
        "component_name"       : component_name,
        "create_missing"       : True,
        "classes"              : [{k: v for k, v in i.items() if k != "realized_by"}
                                  for i in new_intfs or []],
        "realizations"         : list(realizations or []),
        "generalizations"      : [],
        "interface_stereotypes": list(interface_stereotypes or []),
        "interface_pkg"        : interface_pkg or "",
    }
    return op_plans, intf_plan
