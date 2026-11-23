import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
"""
create_ibd.py
-------------
Updates the package-level IBD (StructureDiagram) of a component in Rhapsody:
  - adds ports to their MODULE class and draws them on the module's box
  - contracts provided/required interfaces of THIS component to the ports
  - creates connectors part<->part and component boundary port<->part

plan = {
    "component_name": "rb_sdm_SafeDataMgt",
    "ibd_name"      : "rb_sdm_SafeDataMgtIBD",          # optional
    "module_names"  : ["rb_sdm_SafeDataMgt"],
    "ports": [
        {"name": "rb_sdm_NewPrt", "module": "rb_sdm_SafeDataMgt",
         "provided": ["rb_sdm_GeneralIntf"], "required": []}
    ],
    "links": [
        {"from_obj": "rb_sdm_SafeDataMgt", "from_port": "rb_sdm_NewPrt",   # component/module/part
         "to_obj": "itsRb_sdm_Nvm",        "to_port": "rb_sdm_NvmPrt"}
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
from rhapsody_com import get_sw_model
from read_detailed_ad import find_package_recursive, find_dd_packages


def get_rhapsody():
    rhapsody = rhapsody_connect.get_active_rhapsody()
    project  = rhapsody.activeProject()
    return rhapsody, project


# NOTE: COM objects are compared with "is None" — a COM object can evaluate as
# False in Python, which made "if not port" skip interface contracts and
# "if not main_cls" report a class as missing although it was found.

PORT_SIZE = 12


def find_port(cls, port_name):
    try:
        for i in range(1, cls.ports.Count + 1):
            pt = cls.ports.Item(i)
            if pt.name == port_name:
                return pt
    except Exception:
        pass
    return None


def find_interface(comp_pkg, iface_name):
    """Interface class INSIDE this component (never a same-named one elsewhere)."""
    from create_bdd import find_class_recursive as _find
    return _find(comp_pkg, iface_name)


def contract_interface(port, iface, kind):
    """Contract an interface to a port as provided or required."""
    coll_name = "providedInterfaces" if kind == "provided" else "requiredInterfaces"
    try:
        existing = getattr(port, coll_name)
        for i in range(1, existing.Count + 1):
            if existing.Item(i).name == iface.name:
                return True, f"Already {kind}: {iface.name}"
        if kind == "provided":
            port.addProvidedInterface(iface)
        else:
            port.addRequiredInterface(iface)
        return True, f"Contracted '{iface.name}' as {kind.upper()} on '{port.name}'"
    except Exception as e:
        return False, f"Contract {kind} '{iface.name}' on '{port.name}' failed: {e}"


def _ges(diag, element):
    try:
        coll = diag.getCorrespondingGraphicElements(element)
        return [coll.Item(i) for i in range(1, coll.Count + 1)]
    except Exception:
        return []


def _model_guid(ge):
    try:
        return str(ge.modelObject.GUID)
    except Exception:
        return ""


def _graphical_parent(ge):
    for getter in (lambda g: g.graphicalParent, lambda g: g.getGraphicalParent()):
        try:
            gp = getter(ge)
            if gp is not None:
                return gp
        except Exception:
            pass
    return None


def _bbox(ge):
    """(x, y, w, h) of a node GE, or None if Rhapsody does not expose it."""
    try:
        poly = ge.getGraphicalProperty("Polygon").value
        nums = [int(float(x)) for x in str(poly).replace(";", ",").split(",") if x.strip()]
        pts = list(zip(nums[1::2], nums[2::2]))
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    except Exception:
        pass
    try:
        pos = str(ge.getGraphicalProperty("Position").value).split(",")
        w = int(float(ge.getGraphicalProperty("Width").value))
        h = int(float(ge.getGraphicalProperty("Height").value))
        return int(float(pos[0])), int(float(pos[1])), w, h
    except Exception:
        return None


def _port_ge_on(diag, port, holder_ge):
    """GE of `port` drawn on `holder_ge` (a part/class box)."""
    hg = _model_guid(holder_ge)
    ges = _ges(diag, port)
    for g in ges:
        gp = _graphical_parent(g)
        if gp is not None and hg and _model_guid(gp) == hg:
            return g
    return ges[0] if len(ges) == 1 and holder_ge is None else None


def create_ibd(plan, rhapsody=None):
    """
    Apply IBD changes: ports on their module class (contracting interfaces of
    THIS component), drawn on the module's box, and connectors (part<->part and
    component boundary port<->part). Every problem is returned in "errors".
    """
    from read_ibd import find_ibd, find_component_class
    from create_bdd import find_class_recursive as find_cls

    errors, notes = [], []
    results = {"ports_created": [], "ports_drawn": [], "interfaces_contracted": [],
               "links_created": []}

    def done():
        results["errors"], results["notes"] = errors, notes
        results["success"] = len(errors) == 0
        return results

    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    sw = get_sw_model(project)
    if sw is None:
        sw = project
    component_name = plan.get("component_name", "")
    comp_pkg = find_package_recursive(sw, component_name)
    if comp_pkg is None:
        errors.append(f"IBD: component '{component_name}' not found")
        return done()
    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), dd_pkgs[0] if dd_pkgs else None)
    if dd_pkg is None:
        errors.append("IBD: no DetailedDesign package found")
        return done()

    module_names = plan.get("module_names") or [component_name]
    wanted = ([plan["ibd_name"]] if plan.get("ibd_name") else []) + [f"{m}IBD" for m in module_names]
    ibd = find_ibd(dd_pkg, wanted)
    if ibd is None:
        errors.append(f"IBD: no structure diagram in {dd_pkg.name} - ports/connectors are "
                      f"created in the model but not drawn")
    comp_cls = find_component_class(comp_pkg, component_name)
    print(f"[IBD] Component class: {comp_cls.name if comp_cls is not None else None} | "
          f"IBD: {ibd.name if ibd is not None else None}")

    # Parts of the component, by name and by type (module class)
    part_by_name, parts_by_type = {}, {}
    if comp_cls is not None:
        try:
            parts = comp_cls.getNestedElementsByMetaClass("Part", 1)
            for i in range(1, parts.Count + 1):
                part = parts.Item(i)
                try:
                    oc = part.otherClass
                except Exception:
                    oc = None
                if oc is None:
                    continue
                part_by_name[part.name] = part
                parts_by_type.setdefault(oc.name, []).append(part)
        except Exception as e:
            errors.append(f"IBD: could not read parts of {comp_cls.name}: {e}")
    print(f"[IBD] Parts: {list(part_by_name)}")

    def holder_ges(mod_cls):
        """Boxes on which the module's ports are shown: its parts, or the class itself."""
        if ibd is None:
            return []
        boxes = []
        for part in parts_by_type.get(mod_cls.name, []):
            boxes += _ges(ibd, part)
        return boxes or _ges(ibd, mod_cls)

    # Stereotypes used by existing ports (copied to new ports)
    port_sts = []
    for m in module_names:
        mc = find_cls(dd_pkg, m)
        if mc is None:
            continue
        try:
            for i in range(1, mc.ports.Count + 1):
                sts = mc.ports.Item(i).stereotypes
                port_sts = [sts.Item(k) for k in range(1, sts.Count + 1)]
                if port_sts:
                    break
        except Exception:
            pass
        if port_sts:
            break

    placed = {}   # holder guid -> ports placed so far (for stacking)

    def draw_port(port, mod_cls):
        boxes = holder_ges(mod_cls)
        if ibd is not None and not boxes:
            errors.append(f"IBD: {mod_cls.name} (or a part of it) is not on {ibd.name} - "
                          f"port {port.name} not drawn")
        for box in boxes:
            if _port_ge_on(ibd, port, box) is not None:
                continue
            bb = _bbox(box)
            k = placed.get(_model_guid(box), 0)
            placed[_model_guid(box)] = k + 1
            if bb is not None:
                x, y, w, h = bb
                px = x + w - PORT_SIZE // 2                 # right border of the box
                py = y + 20 + k * (PORT_SIZE + 14)
                py = min(py, y + max(h - PORT_SIZE, 0))
            else:
                px, py = 50, 50 + k * (PORT_SIZE + 14)
                notes.append(f"Port {port.name}: box position unknown - placed at ({px},{py})")
            try:
                ge = ibd.AddNewNodeForElement(port, px, py, PORT_SIZE, PORT_SIZE)
                highlight_ge(ge)
                results["ports_drawn"].append(port.name)
                print(f"[IBD]   Drew port {port.name} on {mod_cls.name} at ({px},{py})")
            except Exception as e:
                errors.append(f"IBD: drawing port {port.name} failed: {e}")

    # ── 1. Ports + interface contracts ───────────────────────────────────────
    for port_def in plan.get("ports", []):
        if not isinstance(port_def, dict) or not port_def.get("name"):
            continue
        port_name = port_def["name"]
        mod_name  = port_def.get("module") or component_name
        mod_cls   = find_cls(dd_pkg, mod_name)
        if mod_cls is None:
            errors.append(f"Port {port_name}: module class '{mod_name}' not found - port not created")
            continue

        port = find_port(mod_cls, port_name)
        if port is None:
            try:
                port = mod_cls.addNewAggr("Port", port_name)
                try: port.addNewAggr("Tag", "LLMGenerated")   # for confirm_changes.py
                except Exception: pass
                for st in port_sts:
                    try: port.addSpecificStereotype(st)
                    except Exception as e: errors.append(f"Port {port_name}: stereotype {st.name}: {e}")
                results["ports_created"].append(f"{mod_name}.{port_name}")
                print(f"[IBD] Created port: {mod_name}.{port_name}")
            except Exception as e:
                errors.append(f"Port {port_name} on {mod_name}: creation failed: {e}")
                continue
        else:
            print(f"[IBD] Port exists: {mod_name}.{port_name}")

        for kind in ("provided", "required"):
            for iface_name in port_def.get(kind, []) or []:
                iface = find_interface(comp_pkg, iface_name)
                if iface is None:
                    errors.append(f"Port {port_name}: interface '{iface_name}' not found in "
                                  f"{component_name} - not contracted")
                    continue
                ok, msg = contract_interface(port, iface, kind)
                print(f"[IBD]   {msg}")
                (results["interfaces_contracted"] if ok else errors).append(msg)

        draw_port(port, mod_cls)

    # ── 2. Connectors ────────────────────────────────────────────────────────
    connector_st = None
    try:
        nested = project.getNestedElementsByMetaClass("Stereotype", 1)
        for i in range(1, nested.Count + 1):
            st = nested.Item(i)
            try:
                if st.name == "connector" and "SysML" in st.getFullPathName():
                    connector_st = st
                    break
            except Exception:
                pass
    except Exception:
        pass

    def resolve_end(obj, port_name):
        """→ (part or None for boundary, port) or raises ValueError."""
        obj = (obj or "").strip()
        if comp_cls is not None and obj in ("", component_name, comp_cls.name):
            # boundary port of the SW component — unless the port belongs to a
            # module with the same name as the component (common in AB12)
            pt = find_port(comp_cls, port_name)
            if pt is not None:
                return None, pt
            if not obj:
                raise ValueError(f"port '{port_name}' not found on component {comp_cls.name}")
        part = part_by_name.get(obj)
        if part is None:
            typed = parts_by_type.get(obj, [])
            if len(typed) == 1:
                part = typed[0]
            elif len(typed) > 1:
                raise ValueError(f"'{obj}' has several parts {[p.name for p in typed]} - name the part")
        if part is None:
            raise ValueError(f"part/module '{obj}' not found in {component_name}")
        pt = find_port(part.otherClass, port_name)
        if pt is None:
            raise ValueError(f"port '{port_name}' not found on {part.otherClass.name}")
        return part, pt

    existing_links = []
    if comp_cls is not None:
        try:
            ls = comp_cls.getNestedElementsByMetaClass("Link", 1)
            existing_links = [ls.Item(i) for i in range(1, ls.Count + 1)]
        except Exception:
            pass

    def link_exists(p1, pt1, p2, pt2):
        want = {(getattr(p1, "name", None), pt1.name), (getattr(p2, "name", None), pt2.name)}
        for lk in existing_links:
            try:
                ends = set()
                for o_attr, p_attr in (("From", "fromPort"), ("to", "toPort")):
                    o = getattr(lk, o_attr)
                    pp = getattr(lk, p_attr)
                    on = None if o is None or o.metaClass == "Port" else o.name
                    pn = pp.name if pp is not None else (o.name if o is not None else None)
                    ends.add((on, pn))
                if ends == want:
                    return True
            except Exception:
                pass
        return False

    for link_def in plan.get("links", []):
        label = (f"{link_def.get('from_obj') or component_name}.{link_def.get('from_port')} -- "
                 f"{link_def.get('to_obj') or component_name}.{link_def.get('to_port')}")
        if comp_cls is None:
            errors.append(f"Connector {label}: SW component class not found")
            continue
        try:
            p1, pt1 = resolve_end(link_def.get("from_obj"), link_def.get("from_port"))
            p2, pt2 = resolve_end(link_def.get("to_obj"), link_def.get("to_port"))
        except ValueError as e:
            errors.append(f"Connector {label}: {e}")
            continue
        if p1 is None and p2 is None:
            errors.append(f"Connector {label}: both ends are component boundary ports")
            continue
        if p1 is not None and p2 is not None and p1.name == p2.name:
            errors.append(f"Connector {label}: both ends on the same part")
            continue
        if link_exists(p1, pt1, p2, pt2):
            notes.append(f"Connector {label}: already exists")
            continue
        try:
            if p1 is not None and p2 is not None:
                link = comp_cls.addLink(p1, p2, None, pt1, pt2)
            else:   # delegation: component boundary port <-> part port
                part, part_port, own_port = (p2, pt2, pt1) if p1 is None else (p1, pt1, pt2)
                link = comp_cls.addLinkToPartViaPort(part, part_port, own_port, None)
        except Exception as e:
            errors.append(f"Connector {label}: creation failed: {e}")
            continue
        if connector_st is not None:
            try: link.addSpecificStereotype(connector_st)
            except Exception as e: errors.append(f"Connector {label}: «connector» stereotype: {e}")
        results["links_created"].append(label)
        print(f"[IBD] Created connector {label}")

        if ibd is None:
            continue
        def end_ge(part, port):
            boxes = _ges(ibd, part) if part is not None else _ges(ibd, comp_cls)
            for box in boxes:
                g = _port_ge_on(ibd, port, box)
                if g is not None:
                    return g
            return None
        g1, g2 = end_ge(p1, pt1), end_ge(p2, pt2)
        if g1 is None or g2 is None:
            errors.append(f"Connector {label}: created, but a port is not drawn on its box in "
                          f"{ibd.name} - connector not drawn")
            continue
        try:
            ibd.AddNewEdgeForElement(link, g1, 0, 0, g2, 0, 0)
            print(f"[IBD]   Connector drawn")
        except Exception as e:
            errors.append(f"Connector {label}: drawing failed: {e}")

    try:
        project.save()
        print("[IBD] Saved")
    except Exception as e:
        errors.append(f"IBD: save failed: {e}")
    return done()


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
