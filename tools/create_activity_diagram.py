"""
create_activity_diagram.py
--------------------------
Creates an Activity Diagram in Rhapsody via COM.

Fixed issues vs previous version:
  - Layout: proper branch/merge Y tracking, no overlap
  - Requirements: uses "requirements" field, correct search root,
                  correct addDependencyTo() COM call
  - Stereotypes: fast lookup from profile package, not diagram walk
  - Transitions: correct COM API (root.addTransition / createDefaultTransition)
  - Guard: correct bracket handling
"""
import re
import win32com.client


# ── Stereotype lookup ─────────────────────────────────────────────────────────

def find_stereotype_object(project, name):
    """
    Find a stereotype by searching AB12SWArchProfile packages directly.
    Much faster than walking all diagram elements.
    """
    def walk(parent, depth=0):
        if depth > 8:
            return None
        try:
            for i in range(1, parent.packages.Count + 1):
                pkg = parent.packages.Item(i)
                # Check stereotypes directly on the package
                try:
                    for j in range(1, pkg.stereotypes.Count + 1):
                        st = pkg.stereotypes.Item(j)
                        if st.name == name:
                            return st
                except:
                    pass
                result = walk(pkg, depth + 1)
                if result:
                    return result
        except:
            pass
        return None

    # First try: walk from project root
    result = walk(project)
    if result:
        return result

    # Fallback: search behavioral diagrams of existing diagrams
    def search_diagrams(parent, depth=0):
        if depth > 6:
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
            for i in range(1, parent.packages.Count + 1):
                result = search_diagrams(parent.packages.Item(i), depth + 1)
                if result:
                    return result
        except:
            pass
        return None

    return search_diagrams(project)


# ── Layout ────────────────────────────────────────────────────────────────────

def compute_layout(plan: dict) -> dict:
    """
    Top-down layout with correct branch/merge handling.
    Tracks Y per branch and aligns merge nodes below the deepest branch.
    Returns {node_id: (x, y, w, h)}
    """
    CX       = 400   # center x
    Y_START  = 80
    STEP     = 130   # vertical step between nodes
    BRANCH_W = 220   # horizontal spacing between branches
    W_ACTION = 200   # action node width
    H_ACTION = 60    # action node height
    W_SMALL  = 60    # decision/merge/fork width
    H_SMALL  = 60

    layout = {}

    # Build adjacency
    out_edges = {}   # id -> [(to, guard)]
    in_edges  = {}   # id -> [from]
    for t in plan.get("transitions", []):
        f, to = t["from"], t["to"]
        out_edges.setdefault(f, []).append((to, t.get("guard", "")))
        in_edges.setdefault(to, []).append(f)

    actions_by_id = {a["id"]: a for a in plan.get("actions", [])}

    def node_size(nid):
        a = actions_by_id.get(nid, {})
        ntype = a.get("type", "action")
        if ntype in ("decision", "merge", "fork", "join"):
            return W_SMALL, H_SMALL
        elif nid in ("initial", "final"):
            return 20, 20
        else:
            return W_ACTION, H_ACTION

    def place(nid, cx, y, visited):
        if nid in visited or nid in layout:
            return y
        visited.add(nid)

        w, h = node_size(nid)
        layout[nid] = (cx - w // 2, y, w, h)
        y += h + STEP

        nexts = out_edges.get(nid, [])
        if len(nexts) == 0:
            return y
        elif len(nexts) == 1:
            return place(nexts[0][0], cx, y, visited)
        else:
            branch_y = y
            max_y = branch_y

            # Separate by type: decisions stay center, others branch left
            main_path   = []  # decision or single continuation
            side_branch = []  # action nodes that branch off

            for child, g in nexts:
                child_type = actions_by_id.get(child, {}).get("type", "action")
                if child_type == "decision":
                    main_path.append((child, g))
                elif child_type in ("merge", "junction"):
                    main_path.append((child, g))
                else:
                    side_branch.append((child, g))

            # If no main path found, treat first child as main
            if not main_path and nexts:
                main_path   = [nexts[0]]
                side_branch = list(nexts[1:])

            # Main path stays at center x
            for child, _ in main_path:
                end_y = place(child, cx, branch_y, visited)
                max_y = max(max_y, end_y)

            # Side branches go left (negative offset)
            n_side = len(side_branch)
            for i, (child, _) in enumerate(side_branch):
                # All side branches go to the left
                child_cx = cx - BRANCH_W * (i + 1)
                end_y = place(child, child_cx, branch_y, visited)
                max_y = max(max_y, end_y)

            return max_y

    place("initial", CX, Y_START, set())

    # Place anything not yet visited (safety net)
    # For nodes with multiple incoming edges (like final after branches),
    # place them below the deepest branch
    y = max((v[1] + v[3] for v in layout.values()), default=Y_START) + STEP
    for nid in list(out_edges.keys()) + [t["to"] for t in plan.get("transitions", [])]:
        if nid not in layout:
            w, h = node_size(nid)
            # Center between incoming nodes
            incoming = in_edges.get(nid, [])
            xs = [layout[s][0] + layout[s][2]//2 for s in incoming if s in layout]
            cx_node = sum(xs)//len(xs) if xs else CX
            layout[nid] = (cx_node - w // 2, y, w, h)
            y += h + STEP

    # ── Post-process: center merge/junction nodes between their incoming branches
    for nid, info in actions_by_id.items():
        if info.get("type") not in ("merge", "fork", "join"):
            continue
        if nid not in layout:
            continue
        incoming = in_edges.get(nid, [])
        if len(incoming) < 2:
            continue
        xs = []
        for src in incoming:
            if src in layout:
                sx, sy, sw, sh = layout[src]
                xs.append(sx + sw // 2)
        if xs:
            avg_cx = sum(xs) // len(xs)
            _, my, mw, mh = layout[nid]
            layout[nid] = (avg_cx - mw // 2, my, mw, mh)

            # Propagate corrected X to all downstream nodes following this merge
            # Walk the linear chain after the merge and re-center each node
            visited_prop = set()
            def propagate_cx(curr_nid, new_cx):
                if curr_nid in visited_prop:
                    return
                visited_prop.add(curr_nid)
                nexts = out_edges.get(curr_nid, [])
                if len(nexts) != 1:
                    return  # stop at branches or dead ends
                child = nexts[0][0]
                if child not in layout:
                    return
                cw, ch = node_size(child)
                _, cy, _, _ = layout[child]
                layout[child] = (new_cx - cw // 2, cy, cw, ch)
                propagate_cx(child, new_cx)

            propagate_cx(nid, avg_cx)

    return layout


# ── Guard helper ──────────────────────────────────────────────────────────────

def set_guard(tr, guard: str):
    if not guard:
        return
    clean = guard.strip()
    if not (clean.startswith("[") and clean.endswith("]")):
        clean = f"[{clean}]"
    try:
        g = tr.getItsGuard()
        if g:
            g.body = clean
            return
    except:
        pass
    try:
        g = tr.addNewAggr("Guard", "")
        tr.setItsGuard(g)
        g2 = tr.getItsGuard()
        (g2 or g).body = clean
    except:
        pass


# ── Requirement finder — reuse find_requirement.py ───────────────────────────

def _get_find_requirement():
    """Lazy import find_requirement_in_ab12requirements from find_requirement.py."""
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "find_requirement",
        os.path.join(os.path.dirname(__file__), "find_requirement.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.find_requirement_in_ab12requirements


def find_requirement(project, req_id, component_name=None):
    """Find a requirement by ID using the scoped find_requirement.py logic."""
    try:
        fn = _get_find_requirement()
        return fn(project, req_id, component_name=component_name)
    except Exception as e:
        print(f"[AD] find_requirement import failed: {e}")
        return None


# ── Main function ─────────────────────────────────────────────────────────────

def create_activity_diagram(dd_pkg, diagram_name: str, plan: dict,
                            rhapsody=None, conventions: dict = None) -> dict:
    """
    Creates an Activity Diagram in the DetailedDesign package.

    plan = {
        "diagram_name": "...",
        "use_case": "...",
        "actions": [
            {"id": "a1", "name": "ShortName", "type": "action",
             "text": "full description", "requirements": ["SRS_SDM_x"]},
            {"id": "d1", "name": "IsValid",   "type": "decision"},
            {"id": "m1", "name": "Merge",     "type": "merge"},
        ],
        "transitions": [
            {"from": "initial", "to": "a1", "guard": ""},
            {"from": "d1",      "to": "a2", "guard": "[yes]"},
        ],
        "swimlanes": []
    }
    """
    errors   = []
    node_map = {}   # plan_id -> COM element
    ge_map   = {}   # plan_id -> GE element

    if conventions is None:
        conventions = {
            "swimlanes_in_analysis": False,
            "fork_join_in_analysis": True,
            "analysis_stereotype"  : None,
            "max_elements_per_ad"  : None,
        }

    allow_swimlanes = conventions.get("swimlanes_in_analysis", False)
    analysis_stereo = conventions.get("analysis_stereotype", None)
    max_elements    = conventions.get("max_elements_per_ad", None)

    print(f"[AD] Creating: {diagram_name} in {dd_pkg.name}")

    # ── Stereotype prefetch ───────────────────────────────────────────────────
    st_analysis  = None
    st_allocated = None
    if analysis_stereo and rhapsody:
        try:
            project     = rhapsody.activeProject()
            st_analysis = find_stereotype_object(project, "Analysis")
            st_allocated = find_stereotype_object(project, "Allocated")
            if st_analysis:
                print(f"[AD] Found Analysis: {st_analysis.getFullPathName()}")
            if st_allocated:
                print(f"[AD] Found Allocated: {st_allocated.getFullPathName()}")
        except Exception as e:
            print(f"[AD] Stereotype lookup failed: {e}")

    # ── 1. Create activity diagram ────────────────────────────────────────────
    try:
        ad   = dd_pkg.addActivityDiagram()
        ad.name = diagram_name
        root = ad.rootState
        print(f"[AD] Activity created: {ad.name}")
    except Exception as e:
        return {"success": False, "diagram_name": diagram_name,
                "nodes": 0, "errors": [f"addActivityDiagram failed: {e}"]}

    # ── 2. Diagram graphics ───────────────────────────────────────────────────
    try:
        diag = ad.getFlowchartDiagram()
        diag.createGraphics()
    except Exception as e:
        errors.append(f"createGraphics failed: {e}")
        diag = None

    # ── 3. Layout ─────────────────────────────────────────────────────────────
    layout = compute_layout(plan)

    # ── 4. Initial node (sentinel) ────────────────────────────────────────────
    node_map["initial"] = True

    # ── 5. Final node ─────────────────────────────────────────────────────────
    try:
        final = root.addActivityFinal()
        node_map["final"] = final
    except Exception as e:
        errors.append(f"Final node failed: {e}")

    # ── 6. Action/Decision/Merge nodes ────────────────────────────────────────
    action_count = 0

    def sanitize(name: str) -> str:
        import re as _re
        name = name.replace("->", "To").replace("::", "_").replace(":", "_")
        name = _re.sub(r"[^\w]", "_", name)
        return name.strip("_") or "Action"

    for action in plan.get("actions", []):
        a_id   = action["id"]
        a_name = sanitize(action.get("name", a_id))
        a_type = action.get("type", "action")
        try:
            if a_type == "decision":
                elem = ad.addNewAggr("Condition", a_name)
            elif a_type in ("merge", "fork", "join"):
                elem = ad.addNewAggr("JunctionConnector", a_name)
            else:
                # Standard action node — use action_N naming
                rhapsody_name = f"action_{action_count}"
                action_count += 1
                elem = root.addState(rhapsody_name)
                # Set entry action text (the description shown in diagram)
                entry_text = action.get("text", "").strip() or a_name
                try:
                    elem.entryAction = entry_text
                except:
                    pass
                # Apply analysis stereotypes
                if st_analysis:
                    try:
                        elem.addSpecificStereotype(st_analysis)
                    except:
                        pass
                if st_allocated:
                    try:
                        elem.addSpecificStereotype(st_allocated)
                    except:
                        pass
                # Apply design/custom stereotype from conventions
                action_st = conventions.get("action_stereotype")
                if action_st:
                    try:
                        elem.addSpecificStereotype(action_st)
                    except:
                        pass
                # AllocatedToModule is an element-reference tag that cannot
                # be set via COM — requires manual selection in Rhapsody UI.
                # Collect for post-creation report.
                alloc_module = action.get("allocated_module", "") or                                plan.get("component_name", "")
                if alloc_module:
                    if "_manual_alloc" not in plan:
                        plan["_manual_alloc"] = []
                    plan["_manual_alloc"].append(
                        {"action": rhapsody_name, "module": alloc_module})
            node_map[a_id] = elem
            print(f"[AD] {a_type}: {a_name}")
        except Exception as e:
            errors.append(f"{a_type} '{a_name}' failed: {e}")

    # ── 7. Draw nodes ─────────────────────────────────────────────────────────
    if diag:
        # Draw DefaultTransition directly — Rhapsody renders this as the
        # initial filled circle + arrow, no ROOT box needed.
        # We draw it BEFORE other nodes so its GE is available for the edge.

        for node_id, elem in node_map.items():
            if elem is True:
                continue
            x, y, w, h = layout.get(node_id, (320, 100, 200, 60))
            try:
                ge = diag.AddNewNodeForElement(elem, x, y, w, h)
                ge_map[node_id] = ge
                # Display settings
                try:
                    mc = elem.metaclass if hasattr(elem, "metaclass") else ""
                    st = str(elem.stateType) if hasattr(elem, "stateType") else ""
                    if mc == "State" and st == "Action":
                        ge.setGraphicalProperty("ShowName", "None")
                        ge.setGraphicalProperty("DisplayedBody", "Action")
                except:
                    pass
                print(f"[AD] Drew {node_id} at ({x},{y}) {w}x{h}")
            except Exception as e:
                errors.append(f"Draw '{node_id}' failed: {e}")

    # ── 8. Swimlanes ──────────────────────────────────────────────────────────
    for sl_def in (plan.get("swimlanes", []) if allow_swimlanes else []):
        sl_name    = sl_def.get("name", "")
        class_name = sl_def.get("class_name", sl_name)
        try:
            sl = ad.addSwimlane(sl_name)
            for i in range(1, dd_pkg.classes.Count + 1):
                cls = dd_pkg.classes.Item(i)
                if cls.name == class_name:
                    sl.represents = cls
                    break
        except Exception as e:
            errors.append(f"Swimlane '{sl_name}' failed: {e}")

    # ── 9. Transitions ────────────────────────────────────────────────────────
    for t in plan.get("transitions", []):
        from_id   = t["from"]
        to_id     = t["to"]
        guard     = t.get("guard", "")
        from_elem = node_map.get(from_id)
        to_elem   = node_map.get(to_id)

        if not from_elem or not to_elem:
            errors.append(f"Transition {from_id}->{to_id}: element missing")
            continue

        try:
            if from_id == "initial":
                # Default transition from initial pseudostate
                tr = root.createDefaultTransition(to_elem)
                set_guard(tr, guard)
                if diag:
                    to_ge = ge_map.get(to_id)
                    if to_ge:
                        tx, ty, tw, th = layout.get(to_id, (300, 230, 200, 60))
                        cx = tx + tw // 2
                        # Draw edge from diagram frame (None) to first action
                        # This is how Rhapsody represents the initial flow arrow
                        try:
                            diag.AddNewEdgeForElement(
                                tr,
                                None, cx, ty - 60,
                                to_ge, cx, ty
                            )
                            print(f"[AD] Initial flow drawn -> {to_id}")
                        except Exception as de:
                            print(f"[AD] Initial edge failed: {de}")
            else:
                # Regular transition — dispatch to get proper typed object
                import win32com.client as _wcc
                from_elem_typed = _wcc.Dispatch(from_elem)
                tr = from_elem_typed.addTransition(to_elem)
                set_guard(tr, guard)
                if diag:
                    from_ge = ge_map.get(from_id)
                    to_ge   = ge_map.get(to_id)
                    if from_ge and to_ge:
                        fx, fy, fw, fh = layout.get(from_id, (320, 100, 200, 60))
                        tx, ty, tw, th = layout.get(to_id,   (320, 200, 200, 60))
                        ge_edge = diag.AddNewEdgeForElement(
                            tr,
                            from_ge, fx + fw // 2, fy + fh,
                            to_ge,   tx + tw // 2, ty
                        )
                        # Set guard label on edge
                        if guard:
                            try:
                                ge_edge.setGraphicalProperty("label", f"[{guard.strip('[]')}]")
                            except:
                                pass

            label = f" [{guard}]" if guard else ""
            print(f"[AD] Transition: {from_id}->{to_id}{label}")
        except Exception as e:
            errors.append(f"Transition {from_id}->{to_id} failed: {e}")

    # ── 10. Requirement linking ───────────────────────────────────────────────
    project = rhapsody.activeProject() if rhapsody else None
    req_ge_map = {}   # req_id -> (GE, x, y) — draw each requirement node only once
    # Requirements column: fixed X to the right of ALL action nodes
    req_col_x = max((x + w for x, y, w, h in layout.values()), default=600) + 40

    for action in plan.get("actions", []):
        a_id  = action["id"]
        reqs  = action.get("requirements", [])
        elem  = node_map.get(a_id)
        if not elem or elem is True or not reqs or not project:
            continue
        for req_id in reqs:
            try:
                req = find_requirement(project, req_id,
                                      component_name=plan.get("component_name"))
                if req:
                    dep = elem.addDependencyTo(req)
                    try:
                        dep.addStereotype("satisfy", "")
                    except:
                        pass
                    if diag:
                        ex, ey, ew, eh = layout.get(a_id, (300, 300, 200, 60))
                        action_ge = ge_map.get(a_id)

                        # Draw requirement node only ONCE — reuse GE for duplicates
                        if req_id not in req_ge_map:
                            req_x = req_col_x
                            req_y = len(req_ge_map) * 60 + 80
                            try:
                                req_ge = diag.AddNewNodeForElement(
                                    req, req_x, req_y, 169, 50)
                                req_ge_map[req_id] = (req_ge, req_x, req_y)
                            except:
                                pass

                        # Draw dependency arrow from action to the shared req GE
                        if req_id in req_ge_map and action_ge:
                            req_ge, req_x, req_y = req_ge_map[req_id]
                            try:
                                diag.AddNewEdgeForElement(
                                    dep,
                                    action_ge, ex + ew,      ey + eh // 2,
                                    req_ge,    req_x,        req_y + 25
                                )
                            except:
                                pass
                    print(f"[AD] Linked {a_id} -> {req_id}")
                else:
                    print(f"[AD] Requirement not found: {req_id}")
            except Exception as e:
                errors.append(f"Req link {a_id}->{req_id} failed: {e}")

    # ── 11. Link diagram to use case ─────────────────────────────────────────
    use_case_name = plan.get("use_case") or plan.get("use_case_name") or ""
    if use_case_name and project:
        try:
            # Search for use case in the component package
            comp_pkg = None
            component_name = plan.get("component_name", "")
            if component_name:
                import sys as _sys
                _sys.path.insert(0, __import__('os').path.dirname(__file__))
                from read_detailed_ad import find_package_recursive as _fpr
                comp_pkg = _fpr(project, component_name)

            uc = None
            if comp_pkg:
                try:
                    for i in range(1, comp_pkg.useCases.Count + 1):
                        if comp_pkg.useCases.Item(i).name == use_case_name:
                            uc = comp_pkg.useCases.Item(i)
                            break
                except:
                    pass

            if uc:
                dep = ad.addDependencyTo(uc)
                try:
                    dep.addStereotype("trace", "")
                except:
                    pass
                print(f"[AD] Linked diagram -> UseCase: {use_case_name}")
            else:
                print(f"[AD] UseCase not found: {use_case_name!r}")
        except Exception as e:
            print(f"[AD] UseCase link failed: {e}")

    # ── 12. Save ──────────────────────────────────────────────────────────────
    try:
        if rhapsody:
            rhapsody.activeProject().save()
        print(f"[AD] Saved")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    hard_errors = [e for e in errors if "failed" in e.lower()
                   and "WARNING" not in e]
    print(f"[AD] Done: {len(node_map)} nodes, {len(ge_map)} drawn, "
          f"{len(hard_errors)} hard errors")

    manual_alloc = plan.get("_manual_alloc", [])
    if manual_alloc:
        print(f"[AD] Manual step required — set AllocatedToModule in Rhapsody UI:")
        for item in manual_alloc:
            print(f"  {item['action']} -> {item['module']}")

    return {
        "success"          : len(hard_errors) == 0,
        "diagram_name"     : diagram_name,
        "nodes"            : len(node_map),
        "drawn"            : len(ge_map),
        "errors"           : errors,
        "manual_steps"     : [
            f"Set AllocatedToModule = {i['module']} on action {i['action']}"
            for i in manual_alloc
        ] if manual_alloc else [],
    }


if __name__ == "__main__":
    import sys
    import argparse
    sys.path.insert(0, __import__('os').path.dirname(__file__))
    from project_config import get_conventions
    from read_detailed_ad import find_package_recursive, find_dd_packages

    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--name",      default="TestAD_Layout")
    args = parser.parse_args()

    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    project  = rhapsody.activeProject()
    conv     = get_conventions(project.name)
    print(f"[AD] Connected: {project.name} ({conv['project_type']})")

    comp_pkg = find_package_recursive(project, args.component)
    if not comp_pkg:
        print(f"[AD] ERROR: component '{args.component}' not found")
        sys.exit(1)

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((p for p in dd_pkgs if "Cfg" not in p.name), None)
    if not dd_pkg:
        print(f"[AD] ERROR: no DetailedDesign package found")
        sys.exit(1)

    print(f"[AD] Found DD package: {dd_pkg.name}")

    TEST_PLAN = {
        "diagram_name"  : args.name,
        "component_name": args.component,
        "use_case"      : "Cyclic check integrity of RAM data",
        "actions": [
            {"id": "a1", "name": "Initialize",  "type": "action",
             "text": "1. Check preconditions\n2. Initialize state",
             "requirements": ["SRS_SDM_217"]},
            {"id": "a2", "name": "Process",     "type": "action",
             "text": "Process the data section",
             "requirements": ["SRS_SDM_220"]},
            {"id": "d1", "name": "IsValid",     "type": "decision", "requirements": []},
            {"id": "a3", "name": "Commit",      "type": "action",
             "text": "Commit and set status valid",
             "requirements": ["SRS_SDM_221"]},
            {"id": "a4", "name": "Rollback",    "type": "action",
             "text": "Rollback and set status faulted",
             "requirements": ["SRS_SDM_221"]},
            {"id": "m1", "name": "MergeResult", "type": "merge", "requirements": []},
            {"id": "a5", "name": "Finalize",    "type": "action",
             "text": "Finalize and save", "requirements": []},
        ],
        "transitions": [
            {"from": "initial", "to": "a1", "guard": ""},
            {"from": "a1",      "to": "a2", "guard": ""},
            {"from": "a2",      "to": "d1", "guard": ""},
            {"from": "d1",      "to": "a3", "guard": "yes"},
            {"from": "d1",      "to": "a4", "guard": "no"},
            {"from": "a3",      "to": "m1", "guard": ""},
            {"from": "a4",      "to": "m1", "guard": ""},
            {"from": "m1",      "to": "a5", "guard": ""},
            {"from": "a5",      "to": "final", "guard": ""},
        ],
        "swimlanes": [],
    }

    result = create_activity_diagram(dd_pkg, args.name, TEST_PLAN, rhapsody,
                                     conventions=conv)
    print(f"\n[AD] Result: {result}")


# ── High-level helper: create or update Analysis AD from Mermaid ───────────────
def create_or_update_ad(component_name: str, usecase: str,
                         mermaid: str, rhapsody=None, req_map: dict = None) -> dict:
    """
    Update (or create) the Analysis Activity Diagram for a use case
    from a Mermaid flowchart string.

    Finds the existing AD in the DD package, deletes its elements,
    and redraws from the Mermaid plan — effectively an update in place.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mermaid_to_ad import from_mermaid
    from read_detailed_ad import find_package_recursive, find_dd_packages

    errors = []
    try:
        import win32com.client
        if rhapsody is None:
            rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
        project  = rhapsody.activeProject()

        from rhapsody_com import get_sw_model
        sw       = get_sw_model(project) or project
        comp_pkg = find_package_recursive(sw, component_name)
        if not comp_pkg:
            return {"success": False, "errors": [f"Component {component_name} not found"]}

        dd_pkgs = find_dd_packages(comp_pkg)
        if not dd_pkgs:
            return {"success": False, "errors": ["No DD package found"]}
        dd_pkg = next((p for p in dd_pkgs if "Cfg" not in p.name), dd_pkgs[0])

        # Find diagram name — normalize usecase name
        uc_norm = usecase.replace(" ", "")
        diag_name = uc_norm + "AD"
        existing  = None
        try:
            bd = dd_pkg.behavioralDiagrams
            for i in range(1, bd.Count + 1):
                d = bd.Item(i)
                d_norm = d.name.lower().replace(" ", "")
                if uc_norm.lower() in d_norm:
                    existing  = d
                    diag_name = d.name
                    break
        except: pass

        # Parse Mermaid to plan, injecting the requirement mapping into each action
        plan = from_mermaid(mermaid, requirements_map=req_map or {})
        plan["diagram_name"] = diag_name

        if existing:
            # Delete existing diagram and recreate
            try:
                existing.deleteFromProject()
                print(f"[ADUpdate] Deleted existing: {diag_name}", file=sys.stderr)
            except Exception as e:
                print(f"[ADUpdate] Could not delete existing AD: {e}", file=sys.stderr)

        result = create_activity_diagram(dd_pkg, diag_name, plan, rhapsody)
        project.save()
        return result

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ADUpdate] ERROR: {e}", file=sys.stderr)
        return {"success": False, "errors": [str(e)]}
