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
import sys
import os
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.project_config import get_conventions


# ── COM helpers ───────────────────────────────────────────────────────────────

def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
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
            if r:
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
            if r:
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
                if result:
                    return result
        except:
            pass
        return None
    return search(project)


# ── BDD Creator ───────────────────────────────────────────────────────────────

def create_bdd(dd_pkg, bdd_name: str, elements: list,
               rhapsody=None, project=None) -> dict:
    """
    Creates a BDD (ObjectModelDiagram) in dd_pkg.

    elements = [
        {"type": "class",         "name": "rb_sdm_SafeDataMgt",    "w": 172, "h": 139},
        {"type": "generalization","from": "rb_sdm_SafeDataMgt", "to": "rb_sdm_SomeIntf"},
    ]
    """
    print(f"[BDD] Creating: {bdd_name}")
    errors  = []
    ge_map   = {}   # name -> ge
    added_ids = set()  # track by GUID to avoid duplicates

    # ── 1. Create diagram ─────────────────────────────────────────────────────
    # Check if diagram already exists — delete and recreate
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            existing = dd_pkg.objectModelDiagrams.Item(i)
            if existing.name == bdd_name:
                existing.deleteFromProject()
                print(f"[BDD] Deleted existing: {bdd_name}")
                break
    except: pass

    try:
        diag = dd_pkg.addObjectModelDiagram(bdd_name)
        print(f"[BDD] Diagram created: {bdd_name}")
    except Exception as e:
        return {"success": False, "diagram_name": bdd_name,
                "errors": [f"addObjectModelDiagram failed: {e}"]}

    # ── 2. Add class GEs ──────────────────────────────────────────────────────
    x, y = 50, 50
    ROW_H = 200
    col   = 0
    MAX_COLS = 5

    for elem in elements:
        if elem.get("type") not in ("class", "class_obj"):
            continue
        name  = elem["name"]
        w     = elem.get("w", 172)
        h     = elem.get("h", 139)

        # Use pre-resolved class object if provided
        if elem.get("type") == "class_obj":
            cls = elem.get("cls")
        else:
            cls = find_class_recursive(dd_pkg, name)
            if not cls:
                cls = find_class_recursive(project, name) if project else None

        if not cls:
            errors.append(f"Class not found: {name}")
            print(f"[BDD] Class not found: {name}")
            continue

        # Skip duplicates by GUID
        try:
            cls_guid = cls.GUID
            if cls_guid in added_ids:
                print(f"[BDD] Skip duplicate: {name}")
                continue
            added_ids.add(cls_guid)
        except: pass

        try:
            ge = diag.AddNewNodeForElement(cls, x, y, w, h)
            ge_map[name] = ge
            # Hide ports to keep BDD blocks clean (matches reference diagrams)
            try:
                ge.hideAllPorts()
            except Exception as hp_e:
                print(f"[BDD] hideAllPorts failed for {name}: {hp_e}")
            print(f"[BDD] Added class: {name} at ({x},{y})")
            try: ge.setGraphicalProperty("FillColor", "255,255,0")
            except: pass
        except Exception as e:
            errors.append(f"AddNewNodeForElement failed for {name}: {e}")
            print(f"[BDD] Draw failed for {name}: {e}")
            continue

        # Layout — grid, spacing based on this element's actual width + margin
        col += 1
        x   += w + 30
        if col >= MAX_COLS:
            col = 0
            x   = 50
            y  += ROW_H

    # ── 3. Add generalization/realization edges ────────────────────────────────
    for elem in elements:
        if elem.get("type") not in ("generalization", "realization"):
            continue
        from_name = elem.get("from", "")
        to_name   = elem.get("to", "")
        from_ge   = ge_map.get(from_name)
        to_ge     = ge_map.get(to_name)

        if not from_ge or not to_ge:
            print(f"[BDD] Skip edge {from_name}->{to_name}: GE missing")
            continue

        # Use pre-resolved class object if provided (avoids duplicate-name bugs)
        from_cls = elem.get("from_cls")
        if not from_cls:
            from_cls = find_class_recursive(dd_pkg, from_name)
            if not from_cls:
                from_cls = find_class_recursive(project, from_name) if project else None

        if not from_cls:
            errors.append(f"Class not found for edge: {from_name}")
            continue

        try:
            gen = None

            # Use baseClass property (NOT .to — that always throws)
            try:
                gens = from_cls.generalizations
                for i in range(1, gens.Count + 1):
                    g = gens.Item(i)
                    try:
                        if g.baseClass.name == to_name:
                            gen = g
                            break
                    except: pass
            except: pass

            # Create new generalization only if truly not found
            if not gen:
                to_cls = elem.get("to_cls")
                if not to_cls:
                    to_cls = find_class_recursive(dd_pkg, to_name)
                if not to_cls and project:
                    to_cls = find_class_recursive(project, to_name)
                if to_cls:
                    try:
                        from_cls.addGeneralization(to_cls)
                        print(f"[BDD] Created generalization: {from_name} -> {to_name}")
                    except Exception as e2:
                        print(f"[BDD] Create generalization failed: {e2}")
                    # Re-fetch using baseClass property
                    try:
                        gens3 = from_cls.generalizations
                        for i in range(1, gens3.Count + 1):
                            g3 = gens3.Item(i)
                            try:
                                if g3.baseClass.name == to_name:
                                    gen = g3
                                    break
                            except: pass
                    except: pass
                    # Apply Realization stereotype if edge type is realization
                    if gen and elem.get("type") == "realization":
                        try:
                            nested = project.getNestedElementsByMetaClass("Stereotype", 1)
                            realization_guid = "GUID 122837c0-ef70-11d4-a08b-00d0b780aafd"
                            for si in range(1, nested.Count + 1):
                                st = nested.Item(si)
                                try:
                                    if st.GUID == realization_guid or \
                                       (st.name == "Realization" and st.getFullPathName() == "Realization"):
                                        gen.addSpecificStereotype(st)
                                        print(f"[BDD] Applied Realization stereotype")
                                        break
                                except: pass
                        except Exception as se:
                            print(f"[BDD] Stereotype failed: {se}")

            if gen:
                try:
                    diag.AddNewEdgeForElement(gen, from_ge, 0, 0, to_ge, 0, 0)
                    print(f"[BDD] Edge: {from_name} -> {to_name}")
                    # Try to show stereotype label on the edge
                    try:
                        ges = diag.getCorrespondingGraphicElements(gen)
                        if ges and ges.Count > 0:
                            ges.Item(1).setGraphicalProperty("ShowStereotype", "True")
                    except: pass
                except Exception as e3:
                    print(f"[BDD] AddNewEdgeForElement failed: {e3}")
            else:
                print(f"[BDD] Generalization not found: {from_name} -> {to_name}")
        except Exception as e:
            errors.append(f"Edge {from_name}->{to_name} failed: {e}")
            print(f"[BDD] Edge failed: {e}")

    # ── 4. Save ───────────────────────────────────────────────────────────────
    try:
        if rhapsody:
            rhapsody.activeProject().save()
        print(f"[BDD] Saved: {bdd_name}")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    print(f"[BDD] Done: {len(ge_map)} elements drawn, {len(errors)} errors")
    return {
        "success"     : len(errors) == 0,
        "diagram_name": bdd_name,
        "drawn"       : len(ge_map),
        "errors"      : errors
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def update_bdd(dd_pkg, bdd_name: str, new_elements: list,
               rhapsody=None, project=None) -> dict:
    """
    Add new elements to an EXISTING BDD diagram.
    Only draws nodes/edges for elements not already in the diagram.
    Places new nodes below existing content.

    new_elements: same format as create_bdd elements list
    """
    print(f"[BDD] Updating: {bdd_name}")
    errors  = []
    ge_map  = {}   # name -> ge (both existing and new)

    # Find existing diagram
    diag = None
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            d = dd_pkg.objectModelDiagrams.Item(i)
            if d.name == bdd_name:
                diag = d
                print(f"[BDD] Found existing diagram: {bdd_name}")
                break
    except: pass

    if not diag:
        print(f"[BDD] Diagram not found: {bdd_name} — creating new")
        return create_bdd(dd_pkg, bdd_name, new_elements, rhapsody, project)

    # Read existing class nodes and their GEs
    existing_names = set()
    existing_class_count = 0
    try:
        elems = diag.getElementsInDiagram()
        for i in range(1, elems.Count + 1):
            e = elems.Item(i)
            try:
                if e.metaClass == "Class":
                    existing_names.add(e.name)
                    existing_class_count += 1
                    # Get GE for this class
                    try:
                        ges = diag.getCorrespondingGraphicElements(e)
                        if ges.Count > 0:
                            ge_map[e.name] = ges.Item(1)
                    except: pass
            except: pass
    except: pass

    print(f"[BDD] Existing classes: {existing_class_count} — {existing_names}")

    # Place new nodes below existing content
    # Estimate: each row ~200px high, start after existing content
    ROW_H    = 200
    MAX_COLS = 5
    x = 50
    y = (existing_class_count // MAX_COLS + 1) * ROW_H + 50
    col = 0

    # Draw only NEW class nodes
    for elem in new_elements:
        if elem.get("type") not in ("class", "class_obj"):
            continue
        name = elem["name"]

        if name in existing_names:
            print(f"[BDD] Class already in diagram: {name}")
            continue  # GE already in ge_map from above

        # Resolve class object
        cls = elem.get("cls")
        if not cls:
            cls = find_class_recursive(dd_pkg, name)
            if not cls and project:
                cls = find_class_recursive(project, name)

        if not cls:
            errors.append(f"Class not found: {name}")
            continue

        w = elem.get("w", 172)
        h = elem.get("h", 139)

        try:
            ge = diag.AddNewNodeForElement(cls, x, y, w, h)
            ge_map[name] = ge
            print(f"[BDD] Added new class: {name} at ({x},{y})")
            try: ge.setGraphicalProperty("FillColor", "255,255,0")
            except: pass
        except Exception as e:
            errors.append(f"AddNewNodeForElement failed for {name}: {e}")
            continue

        col += 1
        x   += w + 30
        if col >= MAX_COLS:
            col = 0; x = 50; y += ROW_H

    # Draw edges for all new generalization/realization elements
    for elem in new_elements:
        if elem.get("type") not in ("generalization", "realization"):
            continue
        from_name = elem.get("from", "")
        to_name   = elem.get("to", "")
        from_ge   = ge_map.get(from_name)
        to_ge     = ge_map.get(to_name)

        if not from_ge or not to_ge:
            print(f"[BDD] Skip edge {from_name}->{to_name}: GE missing")
            continue

        from_cls = elem.get("from_cls") or find_class_recursive(dd_pkg, from_name)
        if not from_cls and project:
            from_cls = find_class_recursive(project, from_name)
        if not from_cls:
            errors.append(f"Class not found for edge: {from_name}")
            continue

        try:
            gen = None
            try:
                gens = from_cls.generalizations
                for i in range(1, gens.Count + 1):
                    g = gens.Item(i)
                    try:
                        if g.baseClass.name == to_name:
                            gen = g; break
                    except: pass
            except: pass

            if not gen:
                to_cls = elem.get("to_cls") or find_class_recursive(dd_pkg, to_name)
                if not to_cls and project:
                    to_cls = find_class_recursive(project, to_name)
                if to_cls:
                    from_cls.addGeneralization(to_cls)
                    print(f"[BDD] Created generalization: {from_name} -> {to_name}")
                    try:
                        gens = from_cls.generalizations
                        for i in range(1, gens.Count + 1):
                            g = gens.Item(i)
                            try:
                                if g.baseClass.name == to_name:
                                    gen = g; break
                            except: pass
                    except: pass
                    # Apply Realization stereotype
                    if gen and elem.get("type") == "realization":
                        try:
                            nested = project.getNestedElementsByMetaClass("Stereotype", 1)
                            for si in range(1, nested.Count + 1):
                                st = nested.Item(si)
                                try:
                                    if st.name == "Realization" and st.getFullPathName() == "Realization":
                                        gen.addSpecificStereotype(st); break
                                except: pass
                        except: pass

            if gen:
                try:
                    diag.AddNewEdgeForElement(gen, from_ge, 0, 0, to_ge, 0, 0)
                    print(f"[BDD] Edge: {from_name} -> {to_name}")
                    try:
                        ges = diag.getCorrespondingGraphicElements(gen)
                        if ges and ges.Count > 0:
                            ges.Item(1).setGraphicalProperty("ShowStereotype", "True")
                    except: pass
                except Exception as e3:
                    print(f"[BDD] Edge failed: {e3}")
        except Exception as e:
            errors.append(f"Edge {from_name}->{to_name} failed: {e}")

    try:
        if rhapsody:
            rhapsody.activeProject().save()
        print(f"[BDD] Saved: {bdd_name}")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    return {
        "success"     : len(errors) == 0,
        "diagram_name": bdd_name,
        "drawn"       : len([k for k in ge_map if k not in existing_names]),
        "errors"      : errors
    }

def create_bdd_from_plan(plan: dict, rhapsody=None, diagram_name: str = None) -> dict:
    """
    Convert plan JSON to elements list and call create_bdd().
    Searches dd_pkg FIRST to find the correct module class (not component class).
    """
    import json as _json
    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    component_name = plan.get("component_name", "")
    comp_pkg = find_package_recursive(project, component_name)
    if not comp_pkg:
        return {"success": False, "error": f"Component not found: {component_name}"}

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((p for p in dd_pkgs if "Cfg" not in p.name), dd_pkgs[0])

    elements = []

    for cls_def in plan.get("classes", []):
        name = cls_def.get("name", "")
        # Search dd_pkg first to get module class, not component class
        cls = find_class_recursive(dd_pkg, name) or find_class_recursive(comp_pkg, name)
        if not cls:
            try:
                cls = dd_pkg.addNewAggr("Class", name)
                print(f"[BDD] Created class: {name}")
                # Tag for confirm_changes.py tracking
                try: cls.addNewAggr("Tag", "LLMGenerated")
                except: pass
                for sn in cls_def.get("stereotypes", []):
                    try:
                        nested = project.getNestedElementsByMetaClass("Stereotype", 1)
                        for i in range(1, nested.Count + 1):
                            st = nested.Item(i)
                            if st.name == sn:
                                cls.addSpecificStereotype(st); break
                    except: pass
            except Exception as e:
                print(f"[BDD] Class failed: {e}"); continue
        # Add operations
        for op_def in cls_def.get("operations", []):
            op_name = op_def.get("name", "")
            try:
                ops = cls.operations
                if not any(ops.Item(i).name == op_name for i in range(1, ops.Count+1)):
                    op = win32com.client.Dispatch(cls.addNewAggr("Operation", op_name))
                    try: op.setReturnTypeDeclaration(op_def.get("return_type","void") or "void")
                    except: pass
                    try: op.visibility = op_def.get("visibility","public")
                    except: pass
                    for arg in op_def.get("arguments", []):
                        try:
                            a = win32com.client.Dispatch(op.addNewAggr("Argument", arg.get("name","arg")))
                            try: a.setTypeDeclaration(arg.get("type",""))
                            except: pass
                        except: pass
                    print(f"[BDD]   Op added: {op_name}({', '.join(a.get('name','') for a in op_def.get('arguments',[]))}) {op_def.get('return_type','void')}")
            except: pass
        # Add attributes
        for attr_def in cls_def.get("attributes", []):
            attr_name = attr_def.get("name", "")
            try:
                attrs = cls.attributes
                if not any(attrs.Item(i).name == attr_name for i in range(1, attrs.Count+1)):
                    attr = win32com.client.Dispatch(cls.addNewAggr("Attribute", attr_name))
                    try: attr.setTypeDeclaration(attr_def.get("type",""))
                    except: pass
            except: pass

        elements.append({"type": "class_obj", "cls": cls, "name": name, "w": 172, "h": 139})

    for real in plan.get("realizations", []):
        specific, general = real.get("specific",""), real.get("general","")
        # Search dd_pkg first for module class
        from_cls = find_class_recursive(dd_pkg, specific) or find_class_recursive(comp_pkg, specific)
        to_cls   = find_class_recursive(dd_pkg, general)  or find_class_recursive(comp_pkg, general)
        elements.append({"type": "realization", "from": specific, "to": general,
                         "from_cls": from_cls, "to_cls": to_cls})

    for gen in plan.get("generalizations", []):
        specific, general = gen.get("specific",""), gen.get("general","")
        from_cls = find_class_recursive(dd_pkg, specific) or find_class_recursive(comp_pkg, specific)
        to_cls   = find_class_recursive(dd_pkg, general)  or find_class_recursive(comp_pkg, general)
        elements.append({"type": "generalization", "from": specific, "to": general,
                         "from_cls": from_cls, "to_cls": to_cls})

    bdd_name = diagram_name or f"{component_name}_BDD"
    # Use update_bdd to add to existing diagram (preserves existing content)
    return update_bdd(dd_pkg, bdd_name, elements, rhapsody, project)


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
    if not comp_pkg:
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
    print(f"[BDD] SW Component class: {sw_comp_cls.name if sw_comp_cls else 'NOT FOUND'}")

    # ── BDD 1: DetailedDesignBDD (composition) ────────────────────────────────
    bdd1_name = f"rb_{short_name}_DetailedDesignBDD"

    bdd1_elements = []
    # SW Component (large block) — from comp_pkg
    if sw_comp_cls:
        bdd1_elements.append(
            {"type": "class_obj", "cls": sw_comp_cls, "name": args.component,
             "w": 355, "h": 139})
    # Modules — from dd_pkg
    for m in modules:
        cls = find_class_recursive(dd_pkg, m["name"])
        if cls:
            bdd1_elements.append(
                {"type": "class_obj", "cls": cls, "name": m["name"],
                 "w": 162, "h": 121})
    # Configs
    for c in configs:
        cls = find_class_recursive(dd_pkg, c["name"])
        if cls:
            bdd1_elements.append(
                {"type": "class_obj", "cls": cls, "name": c["name"],
                 "w": 162, "h": 121})

    result1 = create_bdd(dd_pkg, bdd1_name, bdd1_elements, rhapsody, project)
    print(f"[BDD] BDD1 result: {result1}")

    # ── BDD 2: DetailedDesignInterfacesBDD ────────────────────────────────────
    bdd2_name = f"rb_{short_name}_DetailedDesignInterfacesBDD"

    bdd2_elements = []
    # SW Component
    if sw_comp_cls:
        bdd2_elements.append(
            {"type": "class_obj", "cls": sw_comp_cls, "name": args.component,
             "w": 172, "h": 139})
    # Modules
    for m in modules:
        cls = find_class_recursive(dd_pkg, m["name"])
        if cls:
            bdd2_elements.append(
                {"type": "class_obj", "cls": cls, "name": m["name"],
                 "w": 172, "h": 139})
    # Interfaces — resolve class objects and keep a lookup map
    iface_cls_map = {}
    for iface in interfaces:
        cls = find_class_recursive(dd_pkg, iface["name"])
        if not cls:
            cls = find_class_recursive(comp_pkg, iface["name"])
        if cls:
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
