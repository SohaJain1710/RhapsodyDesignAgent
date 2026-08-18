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
import sys
import os
import json
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_detailed_ad import find_package_recursive, find_dd_packages


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    project  = rhapsody.activeProject()
    return rhapsody, project


def read_ibd_from_package(dd_pkg, comp_cls=None):
    """Read the StructureDiagram from a DetailedDesign package."""
    try:
        sds = dd_pkg.structureDiagrams
        if sds.Count == 0:
            return None
        ibd = sds.Item(1)
    except Exception as e:
        print(f"[ReadIBD] structureDiagrams failed: {e}", file=sys.stderr)
        return None

    elems = ibd.getElementsInDiagram()
    print(f"[ReadIBD] {ibd.name}: {elems.Count} elements", file=sys.stderr)

    objects  = {}   # name -> {metaclass, ports: []}
    ports    = {}   # name -> {owner_object, provided, required}
    links    = []

    # First pass: collect objects and ports
    current_object = None
    for i in range(1, elems.Count + 1):
        e = elems.Item(i)
        try:
            mc = e.metaClass
            name = e.name
        except:
            continue

        if mc in ("Class", "Object"):
            current_object = name
            objects[name] = {"metaClass": mc, "ports": []}

        elif mc == "Port":
            provided = []
            required = []
            try:
                pi = e.providedInterfaces
                provided = [pi.Item(j).name for j in range(1, pi.Count+1)]
            except: pass
            try:
                ri = e.requiredInterfaces
                required = [ri.Item(j).name for j in range(1, ri.Count+1)]
            except: pass
            guid = ""
            try: guid = str(e.GUID)
            except: pass

            port_entry = {
                "name"    : name,
                "owner"   : current_object,
                "provided": provided,
                "required": required,
                "guid"    : guid,
            }
            # Prefer Class owner — if port already registered under a Class,
            # don't overwrite with Object/Instance owner
            existing = ports.get(name)
            if existing:
                existing_owner_mc = objects.get(existing["owner"], {}).get("metaClass", "")
                current_mc = objects.get(current_object, {}).get("metaClass", "")
                if existing_owner_mc == "Class" and current_mc != "Class":
                    # Keep the Class owner, just add this object to its ports
                    if current_object and current_object in objects:
                        objects[current_object]["ports"].append(name)
                    continue
            ports[name] = port_entry
            if current_object and current_object in objects:
                objects[current_object]["ports"].append(name)

    # Read links from the component class (better fromPort/toPort access)
    links    = []
    try:
        # Use passed comp_cls or find it from the owner package
        if comp_cls is None:
            try:
                owner_pkg = dd_pkg.owner
                if owner_pkg:
                    for i in range(1, owner_pkg.classes.Count + 1):
                        cls = owner_pkg.classes.Item(i)
                        try:
                            if cls.name in dd_pkg.name:
                                comp_cls = cls
                                break
                        except: pass
            except: pass

        if comp_cls:
            nested_links = comp_cls.getNestedElementsByMetaClass("Link", 1)
            print(f"[ReadIBD] Links on comp_cls: {nested_links.Count}", file=sys.stderr)
            for i in range(1, nested_links.Count + 1):
                lnk = nested_links.Item(i)
                try:
                    link_name  = lnk.name
                    from_port  = None
                    to_port    = None
                    from_obj   = None
                    to_obj     = None

                    # Use fromElement/toElement metaClass to determine endpoints
                    try:
                        fe = lnk.fromElement
                        te = lnk.toElement
                        fe_mc = fe.metaClass if fe else None
                        te_mc = te.metaClass if te else None

                        if fe_mc == "Object":
                            from_obj = lnk.From.name
                            fp = lnk.fromPort
                            if fp: from_port = fp.name
                        elif fe_mc == "Port":
                            # From is a boundary port on the component Class
                            from_port = lnk.From.name
                            # Always resolve to the Class object (component boundary)
                            from_obj = next(
                                (n for n, d in objects.items()
                                 if d.get("metaClass") == "Class"),
                                ports.get(from_port, {}).get("owner")
                            )

                        if te_mc == "Port":
                            # to is a port — use lnk.to.name as to_port
                            to_port = lnk.to.name
                            # Find which object owns this port
                            to_obj = ports.get(to_port, {}).get("owner")
                            # If to_obj == from_obj, resolve to Class boundary
                            if to_obj == from_obj:
                                for obj_name, obj_data in objects.items():
                                    if (obj_data.get("metaClass") == "Class" and
                                            to_port in obj_data.get("ports", [])):
                                        to_obj = obj_name
                                        break
                        elif te_mc == "Object":
                            to_obj  = lnk.to.name
                            tp = lnk.toPort
                            if tp: to_port = tp.name

                    except: pass

                    # Fallback: use fromPort to resolve from_obj
                    if not from_obj and from_port:
                        from_obj = ports.get(from_port, {}).get("owner")

                    # If from_obj looks like a port name, it means lnk.From
                    # returned a port object — swap to use it as from_port
                    if from_obj and from_obj in ports and not from_port:
                        from_port = from_obj
                        from_obj  = ports.get(from_port, {}).get("owner")

                    links.append({
                        "name"     : link_name,
                        "from_port": from_port,
                        "from_obj" : from_obj,
                        "to_port"  : to_port,
                        "to_obj"   : to_obj,
                    })
                except: pass
    except Exception as e:
        print(f"[ReadIBD] Link read failed: {e}", file=sys.stderr)
        # Fallback: read from diagram elements
        for i in range(1, elems.Count + 1):
            e = elems.Item(i)
            try:
                if e.metaClass != "Link":
                    continue
                links.append({"name": e.name, "from_port": None,
                              "from_obj": None, "to_port": None, "to_obj": None})
            except: pass

    return {
        "diagram_name": ibd.name,
        "objects"     : [{"name": k, **v} for k, v in objects.items()],
        "ports"       : list(ports.values()),
        "links"       : links,
        "summary": {
            "total_objects"  : len(objects),
            "total_ports"    : len(ports),
            "total_links"    : len(links),
            "resolved_links" : sum(1 for l in links if l["from_port"] or l["from_obj"]),
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    _, project = get_rhapsody()

    comp_pkg = find_package_recursive(project, args.component)
    if not comp_pkg:
        print(json.dumps({"error": f"Component '{args.component}' not found"}))
        sys.exit(1)

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), None)
    if not dd_pkg:
        print(json.dumps({"error": "No DetailedDesign package found"}))
        sys.exit(1)

    result = read_ibd_from_package(dd_pkg)
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
