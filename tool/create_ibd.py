from config import RUNTIME_DIR
"""
create_ibd.py
-------------
Creates or updates an IBD (StructureDiagram) in Rhapsody:
  - Adds ports to the component class
  - Contracts provided/required interfaces to ports
  - Creates links between ports and draws them in the IBD

plan = {
    "component_name": "rb_sdm_SafeDataMgt",
    "ports": [
        {
            "name"    : "rb_sdm_NewPrt",
            "provided": ["rb_sdm_GeneralIntf"],
            "required": []
        }
    ],
    "links": [
        {
            "name"     : "rb_sdm_NewPrt_link",
            "from_port": "rb_sdm_NewPrt",
            "from_obj" : "rb_sdm_SafeDataMgt",
            "to_port"  : "rb_sdm_CheckSdmDataStatusPrt",
            "to_obj"   : "itsRb_sdm_SafeDataMgt"
        }
    ]
}
"""
import sys
import os
import json
import argparse
import win32com.client

HIGHLIGHT_COLOR = "255,255,0"  # Yellow — marks newly created elements


def highlight_ge(ge):
    """Highlight a graphic element with yellow background."""
    try:
        ge.setGraphicalProperty("FillColor", HIGHLIGHT_COLOR)
    except: pass



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhapsody_com import get_sw_model, find_class_recursive
from read_detailed_ad import find_package_recursive, find_dd_packages


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    project  = rhapsody.activeProject()
    return rhapsody, project


def walk_classes(parent, depth=0):
    if depth > 8: return []
    cls_list = []
    try:
        for i in range(1, parent.classes.Count + 1):
            cls_list.append(parent.classes.Item(i))
    except: pass
    try:
        for i in range(1, parent.packages.Count + 1):
            cls_list.extend(walk_classes(parent.packages.Item(i), depth+1))
    except: pass
    return cls_list


def find_port(cls, port_name):
    try:
        for i in range(1, cls.ports.Count + 1):
            if cls.ports.Item(i).name == port_name:
                return cls.ports.Item(i)
    except: pass
    return None


def find_interface(sw, pkg, iface_name):
    """Find interface class — search pkg first then whole model."""
    def search(parent, depth=0):
        if depth > 8: return None
        try:
            for i in range(1, parent.classes.Count + 1):
                cls = parent.classes.Item(i)
                if cls.name == iface_name:
                    return cls
        except: pass
        try:
            for i in range(1, parent.packages.Count + 1):
                r = search(parent.packages.Item(i), depth+1)
                if r: return r
        except: pass
        return None

    return search(pkg) or search(sw)


def contract_interface(port, iface, kind):
    """Contract an interface to a port as provided or required."""
    try:
        if kind == "provided":
            existing = port.providedInterfaces
            for i in range(1, existing.Count + 1):
                if existing.Item(i).name == iface.name:
                    return True, f"Already provided: {iface.name}"
            port.addProvidedInterface(iface)
            return True, f"Contracted '{iface.name}' as PROVIDED on '{port.name}'"
        else:
            existing = port.requiredInterfaces
            for i in range(1, existing.Count + 1):
                if existing.Item(i).name == iface.name:
                    return True, f"Already required: {iface.name}"
            port.addRequiredInterface(iface)
            return True, f"Contracted '{iface.name}' as REQUIRED on '{port.name}'"
    except Exception as e:
        return False, f"Contract failed: {e}"


def create_ibd(plan, rhapsody=None):
    """
    Apply IBD changes: add ports, contract interfaces, create links.
    Returns result dict.
    """
    errors   = []
    warnings = []
    results  = {"ports_created": [], "interfaces_contracted": [],
                "links_created": [], "errors": []}

    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    sw = get_sw_model(project) or project
    component_name = plan.get("component_name", "")

    comp_pkg = find_package_recursive(sw, component_name)
    if not comp_pkg:
        return {"success": False, "error": f"Component '{component_name}' not found"}

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), None)
    if not dd_pkg:
        return {"success": False, "error": "No DetailedDesign package found"}

    # Find the main module class in DD package
    main_cls = None
    for cls in walk_classes(dd_pkg):
        try:
            if cls.name == component_name:
                main_cls = cls
                break
        except: pass

    if not main_cls:
        return {"success": False, "error": f"Class '{component_name}' not found in DD package"}

    # Find the StructureDiagram (IBD)
    ibd = None
    try:
        sds = dd_pkg.structureDiagrams
        if sds.Count > 0:
            ibd = sds.Item(1)
    except: pass

    # Build GE map from existing ports using getCorrespondingGraphicElements
    ge_map = {}
    existing_port_names = set()
    existing_port_count = 0
    if ibd:
        try:
            elems = ibd.getElementsInDiagram()
            for i in range(1, elems.Count + 1):
                e = elems.Item(i)
                try:
                    if e.metaClass == "Port":
                        existing_port_names.add(e.name)
                        existing_port_count += 1
                        try:
                            ges = ibd.getCorrespondingGraphicElements(e)
                            if ges.Count > 0:
                                ge_map[e.name] = ges.Item(1)
                        except: pass
                except: pass
        except: pass

    print(f"[IBD] Component: {component_name}, class found: {main_cls.name}")
    print(f"[IBD] IBD found: {ibd.name if ibd else None}")
    print(f"[IBD] Existing ports: {main_cls.ports.Count}, GEs mapped: {len(ge_map)}")

    # Smart placement for new ports
    PORT_W = 120; PORT_H = 40; PORT_GAP = 10; COLS = 5
    new_port_count = [0]  # mutable counter

    def next_port_pos():
        n = existing_port_count + new_port_count[0]
        row = n // COLS
        col = n % COLS
        return 50 + col * (PORT_W + PORT_GAP), 50 + row * (PORT_H + PORT_GAP + 30)

    # ── 1. Create ports ───────────────────────────────────────────────────────
    new_port_ges = {}   # port_name -> GE (for newly drawn ports)

    for port_def in plan.get("ports", []):
        port_name = port_def.get("name", "")
        if not port_name:
            continue

        # Idempotency
        existing = find_port(main_cls, port_name)
        if existing:
            print(f"[IBD] Port already exists: {port_name}")
            results["ports_created"].append(
                {"name": port_name, "created": False, "message": "already exists"})
        else:
            try:
                port = main_cls.addNewAggr("Port", port_name)
                print(f"[IBD] Created port: {port_name}")
                # Tag for confirm_changes.py tracking
                try: port.addNewAggr("Tag", "LLMGenerated")
                except: pass
                results["ports_created"].append(
                    {"name": port_name, "created": True})

                # Draw in IBD only if not already drawn
                if ibd and port_name not in existing_port_names:
                    try:
                        px, py = next_port_pos()
                        ge = ibd.AddNewNodeForElement(port, px, py, PORT_W, PORT_H)
                        new_port_ges[port_name] = ge
                        ge_map[port_name] = ge
                        new_port_count[0] += 1
                        highlight_ge(ge)
                        print(f"[IBD]   Drew port at ({px},{py})")
                    except Exception as de:
                        warnings.append(f"Port draw failed for {port_name}: {de}")
            except Exception as e:
                errors.append(f"Port creation failed for {port_name}: {e}")
                continue

        # ── 2. Contract interfaces ────────────────────────────────────────────
        port = find_port(main_cls, port_name)
        if not port:
            continue

        for iface_name in port_def.get("provided", []):
            iface = find_interface(sw, comp_pkg, iface_name)
            if iface:
                ok, msg = contract_interface(port, iface, "provided")
                print(f"[IBD]   {msg}")
                results["interfaces_contracted"].append(
                    {"port": port_name, "interface": iface_name, "kind": "provided",
                     "success": ok, "message": msg})
            else:
                warnings.append(f"Interface '{iface_name}' not found")

        for iface_name in port_def.get("required", []):
            iface = find_interface(sw, comp_pkg, iface_name)
            if iface:
                ok, msg = contract_interface(port, iface, "required")
                print(f"[IBD]   {msg}")
                results["interfaces_contracted"].append(
                    {"port": port_name, "interface": iface_name, "kind": "required",
                     "success": ok, "message": msg})
            else:
                warnings.append(f"Interface '{iface_name}' not found")

    # ── 3. Create links/connectors ────────────────────────────────────────────
    # Mechanism: addLink(port_part, real_part) on comp_cls creates a visible
    # line in the IBD when combined with AddNewEdgeForElement.
    # port_part: a Part with otherClass=None (port-type part in comp_cls)
    # real_part: a Part with otherClass set (object instance like itsRb_sdm_X)
    comp_cls = None
    try:
        comp_cls = comp_pkg.classes.Item(1)
        print(f"[IBD] Component class: {comp_cls.name}")
    except Exception as e:
        warnings.append(f"Could not get component class: {e}")

    # Find SysML::Blocks::connector stereotype from project stereotypes
    connector_stereotype = None
    try:
        nested = project.getNestedElementsByMetaClass("Stereotype", 1)
        for i in range(1, nested.Count + 1):
            st = nested.Item(i)
            try:
                if st.name == "connector" and "SysML" in st.getFullPathName():
                    connector_stereotype = st
                    print(f"[IBD] Found connector stereotype: {st.getFullPathName()}")
                    break
            except: pass
    except Exception as e:
        print(f"[IBD] Could not find connector stereotype: {e}")

    # Build part maps
    part_map      = {}   # real parts: name -> Part (otherClass != None)
    port_part_map = {}   # port parts: name -> Part (otherClass == None)
    if comp_cls:
        try:
            parts = comp_cls.getNestedElementsByMetaClass("Part", 1)
            for i in range(1, parts.Count + 1):
                part = parts.Item(i)
                try:
                    oc = part.otherClass
                    if oc is not None:
                        part_map[part.name] = part
                    else:
                        port_part_map[part.name] = part
                except:
                    pass
            print(f"[IBD] Object parts: {list(part_map.keys())}")
            print(f"[IBD] Port parts: {list(port_part_map.keys())[:5]}")
        except Exception as e:
            warnings.append(f"Could not get parts: {e}")

    for link_def in plan.get("links", []):
        link_name      = link_def.get("name", "")
        from_port_name = link_def.get("from_port")
        to_port_name   = link_def.get("to_port")
        from_obj_name  = link_def.get("from_obj")
        to_obj_name    = link_def.get("to_obj")

        if not comp_cls:
            errors.append(f"Link '{link_name}': component class not found")
            continue

        # Resolve part instances
        from_part = part_map.get(from_obj_name) if from_obj_name else None
        to_part   = part_map.get(to_obj_name)   if to_obj_name   else None

        if not from_part or not to_part:
            warnings.append(
                f"Link '{link_name}': parts not found "
                f"(from={from_obj_name}, to={to_obj_name}). "
                f"Available: {list(part_map.keys())}")
            continue

        # Skip links between same object (self-connections from boundary ports)
        if from_obj_name == to_obj_name:
            warnings.append(
                f"Link '{link_name}': skipped self-connection "
                f"({from_obj_name}.{from_port_name})")
            continue

        # Skip links with no to_port — insufficient info for connector creation
        if not to_port_name:
            warnings.append(
                f"Link '{link_name}': skipped — to_port unknown "
                f"({from_obj_name}.{from_port_name} -> {to_obj_name})")
            continue

        # Resolve ports from the TYPE CLASS of each part
        from_port = None
        to_port   = None
        try:
            type1 = from_part.otherClass
            for i in range(1, type1.ports.Count + 1):
                if type1.ports.Item(i).name == from_port_name:
                    from_port = type1.ports.Item(i)
                    break
        except: pass
        try:
            type2 = to_part.otherClass
            for i in range(1, type2.ports.Count + 1):
                if type2.ports.Item(i).name == to_port_name:
                    to_port = type2.ports.Item(i)
                    break
        except: pass

        print(f"[IBD] Link: {from_obj_name}.{from_port_name} -> {to_obj_name}.{to_port_name}")
        print(f"[IBD]   from_port={from_port.name if from_port else None} "
              f"to_port={to_port.name if to_port else None}")

        try:
            # addLink with type class ports sets fromPort/toPort correctly
            link = comp_cls.addLink(from_part, to_part, None, from_port, to_port)

            # Apply SysML::Blocks::connector stereotype
            if connector_stereotype:
                try:
                    link.addSpecificStereotype(connector_stereotype)
                    print(f"[IBD] Created connector: {link.name} <<connector>>")
                except:
                    print(f"[IBD] Created link: {link.name}")
            else:
                print(f"[IBD] Created link: {link.name}")

            # Draw edge in IBD using ports from type classes of part instances
            if ibd and from_port_name and to_port_name:
                fp_elem = None
                tp_elem = None
                try:
                    type1 = from_part.otherClass
                    for i in range(1, type1.ports.Count + 1):
                        if type1.ports.Item(i).name == from_port_name:
                            fp_elem = type1.ports.Item(i)
                            break
                except: pass
                try:
                    type2 = to_part.otherClass
                    for i in range(1, type2.ports.Count + 1):
                        if type2.ports.Item(i).name == to_port_name:
                            tp_elem = type2.ports.Item(i)
                            break
                except: pass

                # Use existing GE from ge_map, or draw a new node
                ge1 = ge_map.get(from_port_name)
                ge2 = ge_map.get(to_port_name)

                if not ge1 and fp_elem:
                    px, py = next_port_pos()
                    try:
                        ge1 = ibd.AddNewNodeForElement(fp_elem, px, py, PORT_W, PORT_H)
                        ge_map[from_port_name] = ge1
                        new_port_count[0] += 1
                        highlight_ge(ge1)
                    except: pass

                if not ge2 and tp_elem:
                    px, py = next_port_pos()
                    try:
                        ge2 = ibd.AddNewNodeForElement(tp_elem, px, py, PORT_W, PORT_H)
                        ge_map[to_port_name] = ge2
                        new_port_count[0] += 1
                        highlight_ge(ge2)
                    except: pass

                print(f"[IBD]   GE: {from_port_name}={'✅' if ge1 else '❌'} "
                      f"{to_port_name}={'✅' if ge2 else '❌'}")
                if ge1 and ge2:
                    try:
                        ibd.AddNewEdgeForElement(link, ge1, 0, 0, ge2, 0, 0)
                        print(f"[IBD]   Edge drawn ✅")
                    except Exception as de:
                        warnings.append(f"Edge draw failed: {de}")
                        print(f"[IBD]   Edge draw failed: {de}")

            results["links_created"].append({
                "name"     : link.name,
                "from_port": from_port_name,
                "from_obj" : from_obj_name,
                "to_port"  : to_port_name,
                "to_obj"   : to_obj_name,
                "success"  : True,
            })
        except Exception as e:
            errors.append(f"Link creation failed: {e}")

    # ── 4. Save ───────────────────────────────────────────────────────────────
    try:
        project.save()
        print("[IBD] Saved")
    except Exception as e:
        errors.append(f"Save failed: {e}")

    results["errors"]   = errors
    results["warnings"] = warnings
    results["success"]  = len(errors) == 0

    # Write sidecar for confirm_changes.py
    new_ports = [r["name"] for r in results["ports_created"] if r.get("created")]
    new_links = [r["name"] for r in results["links_created"]]
    if new_ports or new_links:
        import os, json as _json
        sidecar_path = os.path.join(
            r"C:\RhapsodyAIAgent_runtime",
            f"{component_name}_pending_changes.json")
        sidecar = {"ibd_ports": new_ports, "ibd_links": new_links,
                   "bdd_classes": [], "bdd_diagrams": []}
        # Merge with existing sidecar if present
        if os.path.exists(sidecar_path):
            with open(sidecar_path) as sf:
                existing = _json.load(sf)
            sidecar["ibd_ports"] = list(set(existing.get("ibd_ports",[]) + new_ports))
            sidecar["ibd_links"] = list(set(existing.get("ibd_links",[]) + new_links))
            sidecar["bdd_classes"] = existing.get("bdd_classes", [])
            sidecar["bdd_diagrams"] = existing.get("bdd_diagrams", [])
        with open(sidecar_path, "w") as sf:
            _json.dump(sidecar, sf, indent=2)
        print(f"[IBD] Pending changes saved: {sidecar_path}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--args", required=True, help="Path to plan JSON file")
    args = parser.parse_args()

    with open(args.args, encoding="utf-8-sig") as f:
        plan = json.load(f)
    plan["component_name"] = args.component

    rhapsody, _ = get_rhapsody()
    result = create_ibd(plan, rhapsody)
    print(json.dumps(result, indent=2))
