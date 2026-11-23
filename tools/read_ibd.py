"""
read_ibd.py
-----------
Reads the package-level Internal Block Diagram (StructureDiagram) from the
DetailedDesign package. This is the IBD that contains Objects (part instances),
Ports, and Links (connectors between ports/objects).

The link "to" endpoint is not directly accessible via COM — it's inferred
from the link name which follows patterns like:
  {fromObject}_{toPort}
  {fromPort}_{toObject}
  {fromObject}_{toObject}
  connector_N  (anonymous — endpoints inferred from fromPort only)

Usage:
    python read_ibd.py --component rb_sdm_SafeDataMgt
"""
import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
import sys
import os
import json
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_detailed_ad import find_package_recursive, find_dd_packages


def get_rhapsody():
    rhapsody = rhapsody_connect.get_active_rhapsody()
    project  = rhapsody.activeProject()
    return rhapsody, project


def find_ibd(dd_pkg, preferred_names=None):
    """The package-level IBD: a StructureDiagram named like a preferred name
    (e.g. '<module>IBD'), else one whose name contains 'IBD', else the first."""
    try:
        sds = dd_pkg.structureDiagrams
        diags = [sds.Item(i) for i in range(1, sds.Count + 1)]
    except Exception as e:
        print(f"[ReadIBD] structureDiagrams failed: {e}", file=sys.stderr)
        return None
    if not diags:
        return None
    for want in preferred_names or []:
        for d in diags:
            if d.name == want:
                return d
    return next((d for d in diags if "IBD" in d.name.upper()), diags[0])


def find_component_class(comp_pkg, component_name):
    """The SW component class (stereotype AB12SWComponent, else same name)."""
    if comp_pkg is None:
        return None
    try:
        classes = [comp_pkg.classes.Item(i) for i in range(1, comp_pkg.classes.Count + 1)]
    except Exception:
        return None
    for c in classes:
        try:
            sts = [c.stereotypes.Item(k).name for k in range(1, c.stereotypes.Count + 1)]
            if "AB12SWComponent" in sts:
                return c
        except Exception:
            pass
    return next((c for c in classes if c.name == component_name), classes[0] if classes else None)


def _iface_names(coll):
    try:
        return [coll.Item(j).name for j in range(1, coll.Count + 1)]
    except Exception:
        return []


def module_ports(cls) -> dict:
    """{port_name: {"provided": [...], "required": [...]}} of a class."""
    out = {}
    try:
        for i in range(1, cls.ports.Count + 1):
            pt = cls.ports.Item(i)
            out[pt.name] = {"provided": _iface_names(pt.providedInterfaces),
                            "required": _iface_names(pt.requiredInterfaces)}
    except Exception:
        pass
    return out


def read_ibd_from_package(dd_pkg, comp_cls=None, ibd_names=None):
    """Read the package-level IBD (StructureDiagram) of a DetailedDesign package.
    Returns None when the package has no structure diagram."""
    ibd = find_ibd(dd_pkg, ibd_names)
    if ibd is None:
        return None
    try:
        elems = ibd.getElementsInDiagram()
        elem_list = [elems.Item(i) for i in range(1, elems.Count + 1)]
    except Exception as e:
        print(f"[ReadIBD] getElementsInDiagram failed: {e}", file=sys.stderr)
        elem_list = []
    print(f"[ReadIBD] {ibd.name}: {len(elem_list)} elements", file=sys.stderr)

    objects = {}   # name -> {metaClass, type, ports: []}
    ports   = {}   # (owner, name) -> port entry
    type_to_objects = {}   # class name -> [object names typed by it]
    current_object = None

    for e in elem_list:
        try:
            mc, name = e.metaClass, e.name
        except Exception:
            continue
        if mc in ("Class", "Object", "Instance", "Part"):
            current_object = name
            typ = name
            if mc != "Class":
                try:
                    oc = e.otherClass
                    if oc is not None:
                        typ = oc.name
                except Exception:
                    pass
            objects.setdefault(name, {"metaClass": "Class" if mc == "Class" else "Object",
                                      "type": typ, "ports": []})
            type_to_objects.setdefault(typ, []).append(name)

    for e in elem_list:
        try:
            if e.metaClass != "Port":
                continue
            name = e.name
        except Exception:
            continue
        # The port's real owner is its class; show it on the objects of that type
        owner_cls = None
        try:
            ow = e.owner
            if ow is not None:
                owner_cls = ow.name
        except Exception:
            pass
        holders = type_to_objects.get(owner_cls) or ([owner_cls] if owner_cls in objects else [])
        entry = {"name": name, "owner_class": owner_cls,
                 "provided": _iface_names(e.providedInterfaces),
                 "required": _iface_names(e.requiredInterfaces)}
        for h in holders or [None]:
            key = (h, name)
            if key in ports:
                continue
            ports[key] = {**entry, "owner": h}
            if h in objects and name not in objects[h]["ports"]:
                objects[h]["ports"].append(name)

    # Links live on the SW component class
    links = []
    if comp_cls is not None:
        try:
            nested = comp_cls.getNestedElementsByMetaClass("Link", 1)
            for i in range(1, nested.Count + 1):
                lnk = nested.Item(i)
                entry = {"name": "", "from_obj": None, "from_port": None,
                         "to_obj": None, "to_port": None}
                try: entry["name"] = lnk.name
                except Exception: pass
                for side, obj_attr, port_attr in (("from", "from", "fromPort"), ("to", "to", "toPort")):
                    try:
                        o = getattr(lnk, obj_attr if obj_attr != "from" else "From")
                        if o is not None:
                            if o.metaClass == "Port":
                                entry[f"{side}_port"] = o.name
                                entry[f"{side}_obj"] = comp_cls.name   # boundary port
                            else:
                                entry[f"{side}_obj"] = o.name
                    except Exception:
                        pass
                    try:
                        pt = getattr(lnk, port_attr)
                        if pt is not None:
                            entry[f"{side}_port"] = pt.name
                    except Exception:
                        pass
                links.append(entry)
        except Exception as e:
            print(f"[ReadIBD] Link read failed: {e}", file=sys.stderr)

    port_list = list(ports.values())
    return {
        "diagram_name": ibd.name,
        "objects"     : [{"name": k, **v} for k, v in objects.items()],
        "ports"       : port_list,
        "links"       : links,
        "summary": {
            "total_objects" : len(objects),
            "total_ports"   : len(port_list),
            "total_links"   : len(links),
            "resolved_links": sum(1 for l in links if l["from_port"] and l["to_port"]),
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    _, project = get_rhapsody()

    comp_pkg = find_package_recursive(project, args.component)
    if comp_pkg is None:
        print(json.dumps({"error": f"Component '{args.component}' not found"}))
        sys.exit(1)

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), None)
    if dd_pkg is None:
        print(json.dumps({"error": "No DetailedDesign package found"}))
        sys.exit(1)

    result = read_ibd_from_package(dd_pkg, comp_cls=find_component_class(comp_pkg, args.component))
    if not result:
        print(json.dumps({"error": "No StructureDiagram found in DetailedDesign package"}))
        sys.exit(1)

    result["component"] = args.component
    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"[ReadIBD] Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)
