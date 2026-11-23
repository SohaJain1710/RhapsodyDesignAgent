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


# ── IBD validation ────────────────────────────────────────────────────────────

def _as_list(v) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def _split_end(text: str):
    """'obj.port' / 'obj::port' → (obj, port); 'port' → ('', port)."""
    text = str(text or "").strip()
    for sep in ("::", ".", "/"):
        if sep in text:
            a, b = text.rsplit(sep, 1)
            return a.strip(), b.strip()
    return "", text


def normalize_link(link) -> dict | None:
    if not isinstance(link, dict):
        return None
    out = {k: (str(link.get(k) or "").strip()) for k in ("from_obj", "from_port", "to_obj", "to_port")}
    # accept {"from": "obj.port", "to": "obj.port"}
    if not out["from_port"] and link.get("from"):
        out["from_obj"], out["from_port"] = _split_end(link["from"])
    if not out["to_port"] and link.get("to"):
        out["to_obj"], out["to_port"] = _split_end(link["to"])
    if link.get("name"):
        out["name"] = str(link["name"]).strip()
    return out if out["from_port"] and out["to_port"] else None


def validate_ibd(ibd_delta, module_names: list, component_name: str,
                 realizations: list, known_interfaces: list,
                 existing_ports: dict = None) -> dict:
    """Normalize LLM / reviewer IBD proposals.

    - every port gets an owning module ("module"); default = the module that
      realizes one of its provided interfaces, else the single module
    - provided/required become lists; only interfaces that exist or are
      created/realized in this run are kept
    - duplicate ports (same module + name) merged; ports that already exist
      with the same contracts are dropped
    - links need both ports; "from"/"to" strings like "obj.port" accepted
    Returns {"ports", "links", "warnings"}.
    """
    module_names = list(module_names or [])
    existing_ports = existing_ports or {}
    known = set(known_interfaces or []) | {r.get("general") for r in realizations or [] if isinstance(r, dict)}
    realizer = {}
    for r in realizations or []:
        if isinstance(r, dict) and r.get("general") and r.get("specific"):
            realizer.setdefault(r["general"], r["specific"])
    fallback = default_module(module_names, component_name)
    warnings = []

    delta = ibd_delta if isinstance(ibd_delta, dict) else {}
    ports, index, dropped = [], {}, set()
    for raw in delta.get("ports", []) or []:
        p = {"name": raw} if isinstance(raw, str) else dict(raw) if isinstance(raw, dict) else None
        if not p or not str(p.get("name") or "").strip():
            continue
        p["name"] = str(p["name"]).strip()
        p.pop("ibd_level", None)
        prov, req = _as_list(p.get("provided")), _as_list(p.get("required"))
        for kind, lst in (("provided", prov), ("required", req)):
            unknown = [i for i in lst if known and i not in known]
            if unknown:
                warnings.append(f"Port {p['name']}: {kind} interface(s) {unknown} are not part of "
                                f"this design (not created/realized here) - removed.")
            lst[:] = [i for i in lst if not known or i in known]
        p["provided"], p["required"] = prov, req
        if not prov and not req:
            warnings.append(f"Port {p['name']}: no interfaces left - port dropped.")
            dropped.add(p["name"])
            continue

        given = p.get("module") or p.get("owner") or ""
        mod = resolve_module(given, module_names, component_name)
        if mod is None:
            mod = next((realizer[i] for i in prov if i in realizer
                        and resolve_module(realizer[i], module_names, component_name)), None) or fallback
            if mod is None:
                mod = module_names[0]
                warnings.append(f"Port {p['name']}: owning module '{given or '?'}' unknown - "
                                f"assigned to {mod}, please check.")
            elif given:
                warnings.append(f"Port {p['name']}: '{given}' is not a module - placed on {mod}.")
        p["module"] = mod
        p.pop("owner", None)
        for i in prov:
            if i in realizer and realizer[i] != mod:
                warnings.append(f"Port {p['name']} on {mod} provides {i}, which is realized by "
                                f"{realizer[i]} - please check.")
        if not p["name"].endswith("Prt"):
            warnings.append(f"Port {p['name']}: name does not follow the ...Prt convention.")

        ex = existing_ports.get(mod, {}).get(p["name"])
        if ex is not None and set(prov) <= set(ex.get("provided", [])) \
                and set(req) <= set(ex.get("required", [])):
            continue            # already there with these contracts
        key = (mod, p["name"])
        if key in index:        # merge duplicates
            q = index[key]
            q["provided"] = list(dict.fromkeys(q["provided"] + prov))
            q["required"] = list(dict.fromkeys(q["required"] + req))
            continue
        index[key] = p
        ports.append(p)

    links, seen = [], set()
    for raw in delta.get("links", []) or []:
        l = normalize_link(raw)
        if l is None:
            warnings.append(f"Link {raw!r}: both ports (from_port, to_port) are required - dropped.")
            continue
        existing_names = {pn for ports_ in existing_ports.values() for pn in ports_}
        gone = [pn for pn in (l["from_port"], l["to_port"])
                if pn in dropped and pn not in existing_names and pn not in {q["name"] for q in ports}]
        if gone:
            warnings.append(f"Connector {l['from_port']} -- {l['to_port']}: port {gone[0]} was dropped - "
                            f"connector dropped.")
            continue
        key = (l["from_obj"], l["from_port"], l["to_obj"], l["to_port"])
        if key in seen:
            continue
        seen.add(key)
        links.append(l)
    return {"ports": ports, "links": links, "warnings": warnings}
