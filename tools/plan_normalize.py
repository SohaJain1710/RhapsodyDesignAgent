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
    if vis:
        out["visibility"] = vis
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
