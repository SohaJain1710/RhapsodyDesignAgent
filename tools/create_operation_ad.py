"""
create_operation_ad.py
----------------------
Creates an Activity Diagram for a specific operation in Rhapsody.

Usage:
    python create_operation_ad.py --component rb_sdm_SafeDataMgt
        --operation rb_sdm_Init
        --mermaid "C:\\RhapsodyAIAgent_runtime\\rb_sdm_Init_AD.mmd"

The Mermaid file should be a standard flowchart TD/LR:
    flowchart TD
        Start([Start])
        A[Initialize NVM]
        B{NVM ready?}
        C[Set fault flag]
        End([End])
        Start --> A
        A --> B
        B -->|Yes| End
        B -->|No| C
        C --> End
"""
import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
import sys
import os
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhapsody_com import get_sw_model
from read_detailed_ad import find_package_recursive, find_dd_packages
from mermaid_to_ad import from_mermaid
from create_activity_diagram import create_activity_diagram


def get_rhapsody():
    rhapsody = rhapsody_connect.get_active_rhapsody()
    return rhapsody, rhapsody.activeProject()


# NOTE: COM objects are compared with "is None" — a COM object can evaluate as False.

def find_class_recursive(parent, name, depth=0):
    if depth > 8:
        return None
    try:
        for i in range(1, parent.classes.Count + 1):
            if parent.classes.Item(i).name == name:
                return parent.classes.Item(i)
    except Exception:
        pass
    try:
        for i in range(1, parent.packages.Count + 1):
            r = find_class_recursive(parent.packages.Item(i), name, depth + 1)
            if r is not None:
                return r
    except Exception:
        pass
    return None


def find_operation(cls, op_name):
    """Find an operation by name on a class."""
    try:
        ops = cls.operations
        for i in range(1, ops.Count + 1):
            if ops.Item(i).name == op_name:
                return ops.Item(i)
    except Exception:
        pass
    return None


DESIGN_GUID = "GUID 4fcb9b50-1008-4fff-8770-584342da54f9"


def get_design_stereotype(project):
    """AB12DetailedDesign::Design stereotype — by GUID, else by name."""
    by_name = None
    try:
        nested = project.getNestedElementsByMetaClass("Stereotype", 1)
        for i in range(1, nested.Count + 1):
            st = nested.Item(i)
            try:
                if st.GUID == DESIGN_GUID:
                    return st
                if by_name is None and st.name in ("Design", "design"):
                    by_name = st
            except Exception:
                pass
    except Exception:
        pass
    return by_name


def _existing_ads(op):
    found = []
    try:
        nested = op.getNestedElementsRecursive()
        for i in range(1, nested.Count + 1):
            e = nested.Item(i)
            try:
                if e.metaClass == "ActivityDiagram":
                    found.append(e)
            except Exception:
                pass
    except Exception:
        pass
    return found


def _norm_text(t) -> str:
    import re
    return re.sub(r"\W+", " ", str(t or "")).strip().lower()


def find_analysis_ad(dd_pkgs, usecase):
    """Analysis Activity Diagram of a use case ('<usecase>AD', or a name match)."""
    if not usecase:
        return None
    try:
        from read_detailed_ad import find_behavioral_diagram
        d, _ = find_behavioral_diagram(dd_pkgs, usecase + "AD")
        if d is not None:
            return d
    except Exception:
        pass
    want = usecase.lower().replace(" ", "").replace("_", "")
    for pkg in dd_pkgs:
        try:
            bd = pkg.behavioralDiagrams
            for i in range(1, bd.Count + 1):
                d = bd.Item(i)
                n = d.name.lower().replace(" ", "").replace("_", "")
                if want in n or n.startswith(want):
                    return d
        except Exception:
            pass
    return None


def _analysis_states(analysis_ad):
    """{normalized text: state} for the action states of an analysis AD, by
    entry action text and by element name."""
    out = {}
    if analysis_ad is None:
        return out
    try:
        elems = analysis_ad.getElementsInDiagram()
        for i in range(1, elems.Count + 1):
            e = elems.Item(i)
            try:
                mc = getattr(e, "metaClass", None) or getattr(e, "metaclass", None)
                if mc not in ("State", "ActionState", "Action"):
                    continue
                for key in (getattr(e, "entryAction", ""), e.name, str(e.name).replace("_", " ")):
                    k = _norm_text(key)
                    if k and k not in out:
                        out[k] = e
            except Exception:
                pass
    except Exception:
        pass
    return out


def link_to_analysis(elements: dict, analysis_links: dict, analysis_ad) -> list:
    """«trace» dependency from each Operation AD action to the Analysis AD
    action(s) it refines. Returns error strings."""
    errors = []
    if not analysis_links:
        return errors
    if analysis_ad is None:
        return ["Analysis AD of the use case not found - Operation AD actions not traced"]
    states = _analysis_states(analysis_ad)
    for aid, texts in analysis_links.items():
        elem = elements.get(aid)
        if elem is None:
            continue
        for text in texts:
            target = states.get(_norm_text(text))
            if target is None:
                # names are derived from the first 64 chars of the label
                target = next((st for k, st in states.items()
                               if k and _norm_text(text).startswith(k)), None)
            if target is None:
                errors.append(f"action {aid}: analysis action '{text}' not found in "
                              f"{analysis_ad.name} - not traced")
                continue
            try:
                dep = elem.addDependencyTo(target)
                try:
                    dep.addStereotype("trace", "Dependency")
                except Exception:
                    try: dep.addStereotype("trace", "")
                    except Exception: pass
                print(f"[OpAD]   Traced {aid} -> {target.name}")
            except Exception as e:
                errors.append(f"action {aid}: trace to '{text}' failed: {e}")
    return errors


def create_operation_ad(component_name: str, op_name: str, mermaid_str: str,
                        rhapsody=None, module: str = None, replace: bool = False,
                        analysis_links: dict = None, usecase: str = None) -> dict:
    """
    Create the Operation Activity Diagram of `op_name` on its module class.

    module  : module class that owns the operation (default: component name)
    replace : an existing AD is replaced (reviewer edited it); otherwise an
              existing AD is kept and the call is reported as skipped
    analysis_links : {design action id: [analysis action text]} → each action of
              the new AD gets a «trace» dependency to its analysis action(s)
              in the use case's Analysis AD (`usecase`)
    Always returns {"success", "errors", ...}.
    """
    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    def fail(msg):
        return {"success": False, "operation": op_name, "errors": [msg]}

    sw = get_sw_model(project)
    if sw is None:
        sw = project
    comp_pkg = find_package_recursive(sw, component_name)
    if comp_pkg is None:
        return fail(f"component '{component_name}' not found")
    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), dd_pkgs[0] if dd_pkgs else None)
    if dd_pkg is None:
        return fail("no DetailedDesign package found")

    module_name = module or component_name
    module_cls = find_class_recursive(dd_pkg, module_name)
    if module_cls is None:
        return fail(f"module class '{module_name}' not found in {dd_pkg.name}")
    op = find_operation(module_cls, op_name)
    if op is None:
        return fail(f"operation '{op_name}' not found on {module_name}")
    print(f"[OpAD] Found op: {op.name} on {module_cls.name}")

    ad_name = f"{op_name}AD"
    existing = _existing_ads(op)
    if existing and not replace:
        print(f"[OpAD] AD already exists: {existing[0].name} — kept")
        return {"success": True, "skipped": True, "operation": op_name, "errors": [],
                "note": f"AD already exists ({existing[0].name}) - kept"}
    for e in existing:
        try:
            print(f"[OpAD] Replacing existing AD: {e.name}")
            e.deleteFromProject()
        except Exception as ex:
            return fail(f"could not replace existing AD {e.name}: {ex}")

    normalized = mermaid_str.replace("flowchart TD", "graph TD").replace("flowchart LR", "graph LR")
    spec = from_mermaid(normalized)
    if not spec.get("actions"):
        return fail("the Mermaid diagram has no actions - AD not created")
    print(f"[OpAD] Actions: {len(spec.get('actions', []))} "
          f"Transitions: {len(spec.get('transitions', []))}")

    errors = []
    design_st = get_design_stereotype(project)
    if design_st is None:
        errors.append("«Design» stereotype not found in the project - actions created without it")
    conventions = {"action_stereotype": design_st} if design_st is not None else {}
    try:
        result = create_activity_diagram(op, ad_name, spec, rhapsody, conventions)
        project.save()
    except Exception as e:
        return fail(f"create_activity_diagram failed: {e}")
    errors += result.get("errors", []) or []
    if analysis_links:
        errors += link_to_analysis(result.get("elements") or {}, analysis_links,
                                   find_analysis_ad(dd_pkgs, usecase))
        try: project.save()
        except Exception: pass
    print(f"[OpAD] Done: {ad_name}")
    return {
        "success"  : len(errors) == 0,
        "diagram"  : ad_name,
        "operation": op_name,
        "module"   : module_name,
        "replaced" : bool(existing),
        "errors"   : errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--mermaid",   required=True,
                        help="Path to Mermaid flowchart file")
    parser.add_argument("--module",    default=None, help="Owning module class (default: component)")
    parser.add_argument("--replace",   action="store_true", help="Replace an existing AD")
    args = parser.parse_args()

    with open(args.mermaid, encoding="utf-8-sig") as f:
        mermaid_str = f.read()

    import json
    rhapsody, _ = get_rhapsody()
    result = create_operation_ad(args.component, args.operation,
                                 mermaid_str, rhapsody, module=args.module, replace=args.replace)
    print(json.dumps(result, indent=2))
