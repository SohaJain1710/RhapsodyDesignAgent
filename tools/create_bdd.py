"""
create_bdd.py
-------------
Creates Block Definition Diagrams in Rhapsody via COM.
Two BDDs per component:
  1. DetailedDesignBDD  — directed composition (SW component + modules + config)
  2. DetailedDesignInterfacesBDD — interfaces + realization relations

Usage:
    python create_bdd.py --component rb_sdm_SafeDataMgt
"""
import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
import sys
import os
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.project_config import get_conventions


# ── COM helpers ───────────────────────────────────────────────────────────────

def get_rhapsody():
    rhapsody = rhapsody_connect.get_active_rhapsody()
    project  = rhapsody.activeProject()
    if not project:
        raise RuntimeError("No active project")
    return rhapsody, project


def find_package_recursive(parent, name, depth=0, max_depth=12):
    if depth > max_depth:
        return None
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            if pkg.name == name:
                return pkg
            r = find_package_recursive(pkg, name, depth+1)
            if r is not None:
                return r
    except:
        pass
    return None


def find_class_recursive(parent, name, depth=0, max_depth=12):
    if depth > max_depth:
        return None
    try:
        for i in range(1, parent.classes.Count + 1):
            cls = parent.classes.Item(i)
            if cls.name == name:
                return cls
    except:
        pass
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            r = find_class_recursive(pkg, name, depth+1)
            if r is not None:
                return r
    except:
        pass
    return None


def find_dd_packages(parent, keyword="DetailedDesign", depth=0, max_depth=8, results=None):
    if results is None:
        results = []
    if depth > max_depth:
        return results
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            if keyword in pkg.name:
                results.append(pkg)
            find_dd_packages(pkg, keyword, depth+1, max_depth, results)
    except:
        pass
    return results


def find_stereotype_object(project, name):
    """Find stereotype by searching existing model elements."""
    def search(parent, depth=0):
        if depth > 8:
            return None
        try:
            bd = parent.behavioralDiagrams
            for i in range(1, bd.Count + 1):
                d = bd.Item(i)
                try:
                    elems = d.getElementsInDiagram()
                    for j in range(1, elems.Count + 1):
                        e = elems.Item(j)
                        try:
                            sts = e.stereotypes
                            for k in range(1, sts.Count + 1):
                                st = sts.Item(k)
                                if st.name == name:
                                    return st
                        except:
                            pass
                except:
                    pass
        except:
            pass
        try:
            omd = parent.objectModelDiagrams
            for i in range(1, omd.Count + 1):
                d = omd.Item(i)
                try:
                    elems = d.getElementsInDiagram()
                    for j in range(1, elems.Count + 1):
                        e = elems.Item(j)
                        try:
                            sts = e.stereotypes
                            for k in range(1, sts.Count + 1):
                                st = sts.Item(k)
                                if st.name == name:
                                    return st
                        except:
                            pass
                except:
                    pass
        except:
            pass
        try:
            for i in range(1, parent.packages.Count + 1):
                pkg = parent.packages.Item(i)
                result = search(pkg, depth+1)
                if result is not None:
                    return result
        except:
            pass
        return None
    return search(project)


# ── Shared helpers ────────────────────────────────────────────────────────────
# NOTE: COM objects are compared with "is None" — a COM object can evaluate as
# False in Python (e.g. through a default Count property), which silently
# skipped classes/edges when written as "if not obj".

REALIZATION_GUID = "GUID 122837c0-ef70-11d4-a08b-00d0b780aafd"
_stereo_cache: dict = {}


def _guid(elem) -> str:
    try:
        return str(elem.GUID)
    except Exception:
        return ""


def _project_stereotypes(project) -> list:
    key = _guid(project) or id(project)
    if key not in _stereo_cache:
        found = []
        try:
            nested = project.getNestedElementsByMetaClass("Stereotype", 1)
            for i in range(1, nested.Count + 1):
                found.append(nested.Item(i))
        except Exception as e:
            print(f"[BDD] Could not list stereotypes: {e}")
        _stereo_cache[key] = found
    return _stereo_cache[key]


def find_project_stereotype(project, name: str, guid: str = None):
    """Stereotype by GUID (preferred) or by name."""
    by_name = None
    for st in _project_stereotypes(project):
        try:
            if guid and st.GUID == guid:
                return st
            if by_name is None and st.name == name:
                by_name = st
        except Exception:
            pass
    return by_name


def apply_stereotype(elem, project, name: str) -> bool:
    st = find_project_stereotype(project, name)
    if st is None:
        return False
    try:
        elem.addSpecificStereotype(st)
        return True
    except Exception as e:
        print(f"[BDD] addSpecificStereotype({name}) failed: {e}")
        return False


def _find_generalization(from_cls, to_cls, to_name):
    try:
        gens = from_cls.generalizations
        for i in range(1, gens.Count + 1):
            g = gens.Item(i)
            try:
                base = g.baseClass
                if (to_cls is not None and _guid(base) and _guid(base) == _guid(to_cls)) \
                        or base.name == to_name:
                    return g
            except Exception:
                pass
    except Exception:
        pass
    return None


def ensure_generalization(from_cls, to_cls, to_name, realization: bool, project, errors: list):
    """Find or create the generalization from_cls → to_cls; for realizations make
    sure the «Realization» stereotype is applied (else it would be inheritance)."""
    gen = _find_generalization(from_cls, to_cls, to_name)
    if gen is None:
        if to_cls is None:
            errors.append(f"Edge {from_cls.name}->{to_name}: target class not found")
            return None
        try:
            from_cls.addGeneralization(to_cls)
            print(f"[BDD] Created {'realization' if realization else 'generalization'}: "
                  f"{from_cls.name} -> {to_name}")
        except Exception as e:
            errors.append(f"Edge {from_cls.name}->{to_name}: addGeneralization failed: {e}")
            return None
        gen = _find_generalization(from_cls, to_cls, to_name)
        if gen is None:
            errors.append(f"Edge {from_cls.name}->{to_name}: created but not found")
            return None
    if realization:
        try:
            has = any(gen.stereotypes.Item(i).name == "Realization"
                      for i in range(1, gen.stereotypes.Count + 1))
        except Exception:
            has = False
        if not has:
            st = find_project_stereotype(project, "Realization", guid=REALIZATION_GUID)
            if st is None:
                errors.append(f"Edge {from_cls.name}->{to_name}: «Realization» stereotype not "
                              f"found - drawn as generalization")
            else:
                try:
                    gen.addSpecificStereotype(st)
                    print(f"[BDD] Applied Realization stereotype")
                except Exception as e:
                    errors.append(f"Edge {from_cls.name}->{to_name}: Realization stereotype failed: {e}")
    return gen


def ge_for(diag, element):
    """Graphic element of a model element in a diagram (or None)."""
    try:
        ges = diag.getCorrespondingGraphicElements(element)
        if ges is not None and ges.Count > 0:
            return ges.Item(1)
    except Exception:
        pass
    return None


def _draw_edge(diag, gen, from_ge, to_ge, label, errors):
    if ge_for(diag, gen) is not None:
        return                      # already drawn
    try:
        diag.AddNewEdgeForElement(gen, from_ge, 0, 0, to_ge, 0, 0)
        print(f"[BDD] Edge: {label}")
        e = ge_for(diag, gen)
        if e is not None:
            try: e.setGraphicalProperty("ShowStereotype", "True")
            except Exception: pass
    except Exception as e:
        errors.append(f"Edge {label}: AddNewEdgeForElement failed: {e}")


def _resolve(elem, key, dd_pkg, project, name):
    obj = elem.get(key)
    if obj is None:
        obj = find_class_recursive(dd_pkg, name)
    if obj is None and project is not None:
        obj = find_class_recursive(project, name)
    return obj


def _draw_nodes(diag, elements, dd_pkg, project, errors, x, y, hide_ports):
    """Draw class nodes not yet in the diagram. Returns {guid: ge}."""
    ROW_H, MAX_COLS = 200, 5
    col, x0 = 0, x
    for elem in elements:
        if elem.get("type") not in ("class", "class_obj"):
            continue
        name = elem["name"]
        cls = _resolve(elem, "cls", dd_pkg, project, name)
        if cls is None:
            errors.append(f"Class not found: {name}")
            continue
        if ge_for(diag, cls) is not None:
            print(f"[BDD] Class already in diagram: {name}")
            continue
        w, h = elem.get("w", 172), elem.get("h", 139)
        try:
            ge = diag.AddNewNodeForElement(cls, x, y, w, h)
            if hide_ports:
                try: ge.hideAllPorts()
                except Exception as hp_e: print(f"[BDD] hideAllPorts failed for {name}: {hp_e}")
            try: ge.setGraphicalProperty("FillColor", "255,255,0")
            except Exception: pass
            print(f"[BDD] Added class: {name} at ({x},{y})")
        except Exception as e:
            errors.append(f"AddNewNodeForElement failed for {name}: {e}")
            continue
        col += 1
        x   += w + 30
        if col >= MAX_COLS:
            col, x, y = 0, x0, y + ROW_H


def _draw_edges(diag, elements, dd_pkg, project, errors):
    for elem in elements:
        etype = elem.get("type")
        if etype not in ("generalization", "realization"):
            continue
        from_name, to_name = elem.get("from", ""), elem.get("to", "")
        label = f"{from_name} -> {to_name}"
        from_cls = _resolve(elem, "from_cls", dd_pkg, project, from_name)
        to_cls   = _resolve(elem, "to_cls",   dd_pkg, project, to_name)
        if from_cls is None:
            errors.append(f"Edge {label}: source class not found")
            continue
        gen = ensure_generalization(from_cls, to_cls, to_name, etype == "realization",
                                    project, errors)
        if gen is None:
            continue
        # Look up the boxes by MODEL ELEMENT, not by name: the SW component and
        # its module often share a name
        from_ge = ge_for(diag, from_cls)
        to_ge   = ge_for(diag, to_cls) if to_cls is not None else None
        if from_ge is None or to_ge is None:
            errors.append(f"Edge {label}: created in the model but "
                          f"{'source' if from_ge is None else 'target'} is not on diagram {diag.name}")
            continue
        _draw_edge(diag, gen, from_ge, to_ge, label, errors)


# ── BDD Creator ───────────────────────────────────────────────────────────────

def create_bdd(dd_pkg, bdd_name: str, elements: list,
               rhapsody=None, project=None) -> dict:
    """
    Creates (re-creates) a BDD (ObjectModelDiagram) in dd_pkg.

    elements = [
        {"type": "class",         "name": "rb_sdm_SafeDataMgt",    "w": 172, "h": 139},
        {"type": "generalization","from": "rb_sdm_SafeDataMgt", "to": "rb_sdm_SomeIntf"},
    ]
    """
    print(f"[BDD] Creating: {bdd_name}")
    errors = []
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            existing = dd_pkg.objectModelDiagrams.Item(i)
            if existing.name == bdd_name:
                existing.deleteFromProject()
                print(f"[BDD] Deleted existing: {bdd_name}")
                break
    except Exception: pass

    try:
        diag = dd_pkg.addObjectModelDiagram(bdd_name)
        print(f"[BDD] Diagram created: {bdd_name}")
    except Exception as e:
        return {"success": False, "diagram_name": bdd_name,
                "errors": [f"addObjectModelDiagram failed: {e}"]}

    _draw_nodes(diag, elements, dd_pkg, project, errors, 50, 50, hide_ports=True)
    _draw_edges(diag, elements, dd_pkg, project, errors)

    try:
        if rhapsody is not None:
            rhapsody.activeProject().save()
        print(f"[BDD] Saved: {bdd_name}")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    print(f"[BDD] Done: {len(errors)} errors")
    return {"success": len(errors) == 0, "diagram_name": bdd_name, "errors": errors}


def update_bdd(dd_pkg, bdd_name: str, new_elements: list,
               rhapsody=None, project=None) -> dict:
    """
    Add new elements to an EXISTING BDD diagram (creates it if missing).
    Only draws nodes/edges that are not already in the diagram; new nodes are
    placed below the existing content.
    """
    print(f"[BDD] Updating: {bdd_name}")
    errors = []

    diag = None
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            d = dd_pkg.objectModelDiagrams.Item(i)
            if d.name == bdd_name:
                diag = d
                print(f"[BDD] Found existing diagram: {bdd_name}")
                break
    except Exception: pass

    if diag is None:
        print(f"[BDD] Diagram not found: {bdd_name} — creating new")
        return create_bdd(dd_pkg, bdd_name, new_elements, rhapsody, project)

    existing_classes = 0
    try:
        elems = diag.getElementsInDiagram()
        for i in range(1, elems.Count + 1):
            try:
                if elems.Item(i).metaClass == "Class":
                    existing_classes += 1
            except Exception: pass
    except Exception: pass
    print(f"[BDD] Existing classes in diagram: {existing_classes}")

    y = (existing_classes // 5 + 1) * 200 + 50
    _draw_nodes(diag, new_elements, dd_pkg, project, errors, 50, y, hide_ports=True)
    _draw_edges(diag, new_elements, dd_pkg, project, errors)

    try:
        if rhapsody is not None:
            rhapsody.activeProject().save()
        print(f"[BDD] Saved: {bdd_name}")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    return {"success": len(errors) == 0, "diagram_name": bdd_name, "errors": errors}


def _add_members(cls, cls_def, errors, added=None):
    """Add operations / attributes that do not exist yet; report every failure.
    `added` (optional dict) collects what was really created / already there."""
    if added is None:
        added = {}
    cname = cls_def.get("name", "")
    try:
        existing_ops = {cls.operations.Item(i).name for i in range(1, cls.operations.Count + 1)}
    except Exception:
        existing_ops = set()
    for op_def in cls_def.get("operations", []):
        op_name = op_def.get("name", "")
        if not op_name:
            continue
        if op_name in existing_ops:
            added.setdefault("existing_operations", []).append(f"{cname}.{op_name}")
            continue
        try:
            op = win32com.client.Dispatch(cls.addNewAggr("Operation", op_name))
        except Exception as e:
            errors.append(f"{cname}.{op_name}: could not create operation: {e}")
            continue
        existing_ops.add(op_name)
        added.setdefault("operations", []).append(f"{cname}.{op_name}")
        try: op.setReturnTypeDeclaration(op_def.get("return_type", "void") or "void")
        except Exception as e: errors.append(f"{cname}.{op_name}: return type: {e}")
        if op_def.get("visibility"):
            try: op.visibility = op_def["visibility"]
            except Exception as e: errors.append(f"{cname}.{op_name}: visibility: {e}")
        for arg in op_def.get("arguments", []):
            try:
                a = win32com.client.Dispatch(op.addNewAggr("Argument", arg.get("name", "arg")))
                if arg.get("type"):
                    a.setTypeDeclaration(arg["type"])
            except Exception as e:
                errors.append(f"{cname}.{op_name}: argument {arg.get('name')}: {e}")
        args_txt = ", ".join(f"{x.get('name','')}: {x.get('type','')}" for x in op_def.get("arguments", []))
        print(f"[BDD]   Op added: {cname}.{op_name}({args_txt}) {op_def.get('return_type','void')}")

    try:
        existing_attrs = {cls.attributes.Item(i).name for i in range(1, cls.attributes.Count + 1)}
    except Exception:
        existing_attrs = set()
    for attr_def in cls_def.get("attributes", []):
        attr_name = attr_def.get("name", "")
        if not attr_name or attr_name in existing_attrs:
            continue
        try:
            attr = win32com.client.Dispatch(cls.addNewAggr("Attribute", attr_name))
            existing_attrs.add(attr_name)
            added.setdefault("attributes", []).append(f"{cname}.{attr_name}")
            if attr_def.get("type"):
                attr.setTypeDeclaration(attr_def["type"])
        except Exception as e:
            errors.append(f"{cname}.{attr_name}: attribute: {e}")


def create_bdd_from_plan(plan: dict, rhapsody=None, diagram_name: str = None) -> dict:
    """
    Apply a plan (classes + realizations/generalizations) and draw it on a BDD.

    plan keys:
      component_name, classes, realizations, generalizations
      create_missing        False → classes must exist (module classes); never invent one
      interface_stereotypes stereotype(s) for NEW classes when theirs do not exist
      interface_pkg         package name (inside the component) for NEW classes
    """
    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    errors = []
    component_name = plan.get("component_name", "")
    comp_pkg = find_package_recursive(project, component_name)
    if comp_pkg is None:
        return {"success": False, "errors": [f"Component not found: {component_name}"]}

    dd_pkgs = find_dd_packages(comp_pkg)
    if not dd_pkgs:
        return {"success": False, "errors": [f"No DetailedDesign package in {component_name}"]}
    dd_pkg = next((p for p in dd_pkgs if "Cfg" not in p.name), dd_pkgs[0])

    new_cls_pkg = dd_pkg
    if plan.get("interface_pkg"):
        pkg = find_package_recursive(comp_pkg, plan["interface_pkg"])
        if pkg is not None:
            new_cls_pkg = pkg
        else:
            print(f"[BDD] Interface package '{plan['interface_pkg']}' not found — using {dd_pkg.name}")

    create_missing = plan.get("create_missing", True)
    changes = {}          # what this call really created (for the change summary)
    fallback_sts   = plan.get("interface_stereotypes") or []
    elements = []

    from plan_normalize import normalize_classes
    for cls_def in normalize_classes(plan.get("classes", [])):
        name = cls_def.get("name", "")
        # Search dd_pkg first to get the module class, not the SW component class
        cls = find_class_recursive(dd_pkg, name)
        if cls is None:
            cls = find_class_recursive(comp_pkg, name)
        if cls is None:
            if not create_missing:
                errors.append(f"Class {name} not found in {component_name} — its operations "
                              f"were NOT added (no new module class is created)")
                continue
            try:
                cls = new_cls_pkg.addNewAggr("Class", name)
                changes.setdefault("classes", []).append(name)
                print(f"[BDD] Created class: {name} in {new_cls_pkg.name}")
                try: cls.addNewAggr("Tag", "LLMGenerated")   # for confirm_changes.py
                except Exception: pass
                applied = [sn for sn in cls_def.get("stereotypes", [])
                           if apply_stereotype(cls, project, sn)]
                if not applied:
                    applied = [sn for sn in fallback_sts if apply_stereotype(cls, project, sn)]
                if applied:
                    print(f"[BDD]   Stereotype(s): {applied}")
                else:
                    wanted = cls_def.get("stereotypes", []) or fallback_sts
                    errors.append(f"Class {name}: stereotype {wanted or '(none given)'} not found "
                                  f"in the project — created without stereotype")
            except Exception as e:
                errors.append(f"Class {name}: could not create: {e}")
                continue
        _add_members(cls, cls_def, errors, changes)
        elements.append({"type": "class_obj", "cls": cls, "name": name, "w": 172, "h": 139})

    for kind in ("realizations", "generalizations"):
        etype = kind[:-1]
        for rel in plan.get(kind, []):
            if not isinstance(rel, dict) or not rel.get("specific") or not rel.get("general"):
                print(f"[BDD] Skipping malformed {etype}: {rel!r}")
                continue
            specific, general = rel["specific"], rel["general"]
            from_cls = find_class_recursive(dd_pkg, specific)
            if from_cls is None:
                from_cls = find_class_recursive(comp_pkg, specific)
            to_cls = find_class_recursive(dd_pkg, general)
            if to_cls is None:
                to_cls = find_class_recursive(comp_pkg, general)
            if from_cls is None:
                errors.append(f"{etype.capitalize()} {specific} -> {general}: "
                              f"'{specific}' is not a class of {component_name}")
                continue
            # make sure both ends are on the diagram, otherwise the edge cannot be drawn
            for nm, obj in ((specific, from_cls), (general, to_cls)):
                if obj is not None and not any(e.get("cls") is obj for e in elements):
                    elements.append({"type": "class_obj", "cls": obj, "name": nm, "w": 172, "h": 139})
            elements.append({"type": etype, "from": specific, "to": general,
                             "from_cls": from_cls, "to_cls": to_cls,
                             "pre_exists": _find_generalization(from_cls, to_cls, general) is not None})

    bdd_name = diagram_name or f"{component_name}_BDD"
    res = update_bdd(dd_pkg, bdd_name, elements, rhapsody, project)
    for e in elements:
        if e.get("type") in ("realization", "generalization") and not e.get("pre_exists") \
                and e.get("from_cls") is not None \
                and _find_generalization(e["from_cls"], e.get("to_cls"), e["to"]) is not None:
            arrow = "..|>" if e["type"] == "realization" else "--|>"
            changes.setdefault("relations", []).append(f"{e['from']} {arrow} {e['to']}")
    res["errors"] = errors + (res.get("errors") or [])
    res["success"] = len(res["errors"]) == 0
    res["changes"] = changes
    return res


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--plan",      default=None)
    parser.add_argument("--diagram",   default=None)
    args = parser.parse_args()

    rhapsody, project = get_rhapsody()

    if args.plan:
        with open(args.plan, encoding="utf-8-sig") as f:
            plan = json.load(f)
        plan["component_name"] = args.component
        result = create_bdd_from_plan(plan, rhapsody, diagram_name=args.diagram)
        print(json.dumps(result, indent=2))
        import sys; sys.exit(0)

    conv       = get_conventions(project.name)
    skip_roots = conv.get("skip_roots", [])

    print(f"[BDD] Project: {project.name} -> {conv['project_type']}")
    print(f"[BDD] Component: {args.component}")

    # Find component and DD package
    comp_pkg = find_package_recursive(project, args.component)
    if comp_pkg is None:
        print(f"[BDD] ERROR: Component '{args.component}' not found")
        sys.exit(1)

    dd_pkgs = find_dd_packages(comp_pkg)
    if not dd_pkgs:
        print(f"[BDD] ERROR: No DetailedDesign packages found")
        sys.exit(1)

    # Use first non-Cfg DD package
    dd_pkg = next((p for p in dd_pkgs if "Cfg" not in p.name), dd_pkgs[0])
    print(f"[BDD] Target DD package: {dd_pkg.name}")

    # Read existing classes
    from tools.read_detailed_design import read_classes_from_pkg
    all_classes = read_classes_from_pkg(dd_pkg, conv)
    modules    = [c for c in all_classes if c["is_module"]]
    interfaces = [c for c in all_classes if c["is_interface"]]
    configs    = [c for c in all_classes if c["is_config"]]

    print(f"[BDD] Modules: {len(modules)}, Interfaces: {len(interfaces)}, Configs: {len(configs)}")

    # Short name for diagram naming
    parts      = args.component.split("_")
    short_name = parts[1] if len(parts) >= 2 else parts[0]

    # Find the SW component class (in comp_pkg, not dd_pkg)
    sw_comp_cls = find_class_recursive(comp_pkg, args.component)
    print(f"[BDD] SW Component class: {sw_comp_cls.name if sw_comp_cls is not None else 'NOT FOUND'}")

    # ── BDD 1: DetailedDesignBDD (composition) ────────────────────────────────
    bdd1_name = f"rb_{short_name}_DetailedDesignBDD"

    bdd1_elements = []
    # SW Component (large block) — from comp_pkg
    if sw_comp_cls is not None:
        bdd1_elements.append(
            {"type": "class_obj", "cls": sw_comp_cls, "name": args.component,
             "w": 355, "h": 139})
    # Modules — from dd_pkg
    for m in modules:
        cls = find_class_recursive(dd_pkg, m["name"])
        if cls is not None:
            bdd1_elements.append(
                {"type": "class_obj", "cls": cls, "name": m["name"],
                 "w": 162, "h": 121})
    # Configs
    for c in configs:
        cls = find_class_recursive(dd_pkg, c["name"])
        if cls is not None:
            bdd1_elements.append(
                {"type": "class_obj", "cls": cls, "name": c["name"],
                 "w": 162, "h": 121})

    result1 = create_bdd(dd_pkg, bdd1_name, bdd1_elements, rhapsody, project)
    print(f"[BDD] BDD1 result: {result1}")

    # ── BDD 2: DetailedDesignInterfacesBDD ────────────────────────────────────
    bdd2_name = f"rb_{short_name}_DetailedDesignInterfacesBDD"

    bdd2_elements = []
    # SW Component
    if sw_comp_cls is not None:
        bdd2_elements.append(
            {"type": "class_obj", "cls": sw_comp_cls, "name": args.component,
             "w": 172, "h": 139})
    # Modules
    for m in modules:
        cls = find_class_recursive(dd_pkg, m["name"])
        if cls is not None:
            bdd2_elements.append(
                {"type": "class_obj", "cls": cls, "name": m["name"],
                 "w": 172, "h": 139})
    # Interfaces — resolve class objects and keep a lookup map
    iface_cls_map = {}
    for iface in interfaces:
        cls = find_class_recursive(dd_pkg, iface["name"])
        if cls is None:
            cls = find_class_recursive(comp_pkg, iface["name"])
        if cls is not None:
            iface_cls_map[iface["name"]] = cls
            bdd2_elements.append(
                {"type": "class_obj", "cls": cls, "name": iface["name"],
                 "w": 172, "h": 139})
    # Realization edges from SW component to interfaces — pass resolved objects
    for iface in interfaces:
        to_cls = iface_cls_map.get(iface["name"])
        bdd2_elements.append(
            {"type": "generalization",
             "from": args.component, "to": iface["name"],
             "from_cls": sw_comp_cls, "to_cls": to_cls})

    result2 = create_bdd(dd_pkg, bdd2_name, bdd2_elements, rhapsody, project)
    print(f"[BDD] BDD2 result: {result2}")
