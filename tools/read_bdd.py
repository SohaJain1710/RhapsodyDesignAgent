"""
read_bdd.py
-----------
Reads Block Definition Diagrams (ObjectModelDiagram) from a Rhapsody
DetailedDesign package and returns structured JSON.

Usage:
    python read_bdd.py --component rb_sdm_SafeDataMgt
    python read_bdd.py --component rb_sdm_SafeDataMgt --diagram rb_sdm_DetailedDesignBDD
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


def read_operation(op):
    """Read a single operation."""
    result = {"name": op.name, "arguments": [], "return_type": "void"}
    try:
        result["return_type"] = op.getReturnTypeDeclaration() or "void"
    except: pass
    try:
        args = op.arguments
        for i in range(1, args.Count + 1):
            arg = args.Item(i)
            arg_entry = {"name": arg.name, "type": ""}
            for prop in ["typeOf", "type"]:
                try:
                    t = getattr(arg, prop)
                    if t:
                        if hasattr(t, "name") and not str(t).startswith("<"):
                            name = t.name
                        elif isinstance(t, str):
                            name = t
                        else:
                            # COM object — get .name safely
                            try: name = t.name
                            except: name = ""
                        if name and name != "None" and not name.startswith("<"):
                            arg_entry["type"] = name
                            break
                except: pass
            if not arg_entry["type"]:
                try: arg_entry["type"] = arg.declaration or ""
                except: pass
            result["arguments"].append(arg_entry)
    except: pass
    try:
        sts = op.stereotypes
        result["stereotypes"] = [sts.Item(i).name for i in range(1, sts.Count+1)]
    except:
        result["stereotypes"] = []
    try:
        result["visibility"] = op.visibility
    except:
        result["visibility"] = "public"
    return result


def read_attribute(attr):
    """Read a single attribute."""
    result = {"name": attr.name, "type": ""}
    for prop in ["typeOf", "type"]:
        try:
            t = getattr(attr, prop)
            if t:
                if isinstance(t, str):
                    name = t
                else:
                    try: name = t.name
                    except: name = ""
                if name and name != "None" and not name.startswith("<"):
                    result["type"] = name
                    break
        except: pass
    if not result["type"]:
        try: result["type"] = attr.getTypeDeclaration() or ""
        except: pass
    try:
        sts = attr.stereotypes
        result["stereotypes"] = [sts.Item(i).name for i in range(1, sts.Count+1)]
    except:
        result["stereotypes"] = []
    try:
        result["visibility"] = attr.visibility
    except:
        result["visibility"] = "private"
    try:
        result["default_value"] = attr.defaultValue or ""
    except:
        result["default_value"] = ""
    return result


def read_class(cls):
    """Read a class with all operations and attributes."""
    operations  = []
    attributes  = []
    stereotypes = []

    try:
        sts = cls.stereotypes
        stereotypes = [sts.Item(i).name for i in range(1, sts.Count+1)]
    except: pass

    try:
        ops = cls.operations
        for i in range(1, ops.Count + 1):
            try: operations.append(read_operation(ops.Item(i)))
            except: pass
    except: pass

    try:
        attrs = cls.attributes
        for i in range(1, attrs.Count + 1):
            try: attributes.append(read_attribute(attrs.Item(i)))
            except: pass
    except: pass

    guid = ""
    try: guid = str(cls.GUID)
    except: pass

    return {
        "name"       : cls.name,
        "guid"       : guid,
        "stereotypes": stereotypes,
        "operations" : operations,
        "attributes" : attributes,
        "summary": {
            "total_operations": len(operations),
            "total_attributes": len(attributes),
        }
    }


def find_unrealized_interfaces(dd_pkg, comp_pkg=None) -> dict:
    """
    Compare all interface classes in the BDDs against what the module class
    actually realizes. Returns interfaces present in arch but not yet realized.

    Returns:
    {
        "all_interfaces": ["rb_sdm_EolHandlingIntf", ...],
        "realized": ["rb_sdm_EolHandlingIntf", ...],
        "unrealized": ["rb_sdm_NewIntf", ...]  <- LLM should realize these
    }
    """
    # Get all interface classes from the InterfacesBDD
    all_interfaces = []
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            d = dd_pkg.objectModelDiagrams.Item(i)
            if "Interface" in d.name:
                elems = d.getElementsInDiagram()
                for j in range(1, elems.Count + 1):
                    e = elems.Item(j)
                    try:
                        if e.metaClass == "Class":
                            sts = [e.stereotypes.Item(k).name
                                   for k in range(1, e.stereotypes.Count + 1)]
                            if any("Intf" in e.name or "Interface" in s
                                   for s in sts) or "Intf" in e.name:
                                if e.name not in all_interfaces:
                                    all_interfaces.append(e.name)
                    except: pass
    except Exception as ex:
        print(f"[ReadBDD] find_unrealized: {ex}", file=sys.stderr)

    # Get realized interfaces from the module class
    realized = []
    try:
        def walk_cls(parent, depth=0):
            if depth > 8: return []
            result = []
            try:
                for i in range(1, parent.classes.Count + 1):
                    result.append(parent.classes.Item(i))
            except: pass
            try:
                for i in range(1, parent.packages.Count + 1):
                    result.extend(walk_cls(parent.packages.Item(i), depth + 1))
            except: pass
            return result

        search_pkg = dd_pkg if dd_pkg else comp_pkg
        for cls in walk_cls(search_pkg):
            try:
                sts = [cls.stereotypes.Item(i).name
                       for i in range(1, cls.stereotypes.Count + 1)]
                if "AB12Module" in sts:
                    gens = cls.generalizations
                    for i in range(1, gens.Count + 1):
                        g = gens.Item(i)
                        try:
                            realized.append(g.baseClass.name)
                        except: pass
            except: pass
    except: pass

    unrealized = [i for i in all_interfaces if i not in realized]

    return {
        "all_interfaces": all_interfaces,
        "realized"      : realized,
        "unrealized"    : unrealized,
        "summary": {
            "total_interfaces": len(all_interfaces),
            "realized_count"  : len(realized),
            "unrealized_count": len(unrealized),
        }
    }


def read_bdd_from_package(dd_pkg, diagram_name=None):
    """Read all ObjectModelDiagrams from a DD package."""
    results = []
    try:
        diags = dd_pkg.objectModelDiagrams
    except Exception as e:
        print(f"[ReadBDD] objectModelDiagrams failed: {e}", file=sys.stderr)
        return results

    for d_idx in range(1, diags.Count + 1):
        diag = diags.Item(d_idx)
        try:
            name = diag.name
        except:
            continue

        if diagram_name and name != diagram_name:
            continue

        print(f"[ReadBDD] Reading: {name}", file=sys.stderr)

        classes       = []
        objects       = []
        dependencies  = []
        generalizations = []

        try:
            elems = diag.getElementsInDiagram()
            seen_classes = set()  # avoid duplicate class reads

            for i in range(1, elems.Count + 1):
                e = elems.Item(i)
                try:
                    mc = e.metaClass
                except:
                    continue

                if mc == "Class":
                    guid = ""
                    try: guid = str(e.GUID)
                    except: pass
                    if guid and guid in seen_classes:
                        continue
                    if guid:
                        seen_classes.add(guid)
                    cls_data = read_class(e)
                    # Tag with role based on stereotype
                    if "AB12SWComponent" in cls_data["stereotypes"]:
                        cls_data["role"] = "component"
                    elif "AB12Module" in cls_data["stereotypes"]:
                        cls_data["role"] = "module"
                    elif "AB12ConfigFile" in cls_data["stereotypes"]:
                        cls_data["role"] = "config"
                    else:
                        cls_data["role"] = "class"
                    classes.append(cls_data)

                elif mc == "Object":
                    obj = {"name": e.name}
                    try:
                        oc = e.otherClass
                        obj["type"] = oc.name if oc else None
                    except: obj["type"] = None
                    try: obj["guid"] = str(e.GUID)
                    except: pass
                    objects.append(obj)

                elif mc == "Dependency":
                    dep = {}
                    try:
                        dep["from"] = e.dependent.name
                        dep["to"]   = e.dependsOn.name
                    except: pass
                    try:
                        sts = e.stereotypes
                        dep["stereotypes"] = [sts.Item(j).name for j in range(1, sts.Count+1)]
                    except:
                        dep["stereotypes"] = []
                    dependencies.append(dep)

                elif mc == "Generalization":
                    gen = {"name": e.name}
                    try:
                        gen["specific"] = e.derivedClass.name  # class that realizes
                        gen["general"]  = e.baseClass.name     # interface being realized
                        gen["type"]     = e.userDefinedMetaClass or "Generalization"
                    except: pass
                    generalizations.append(gen)

        except Exception as ex:
            print(f"[ReadBDD] getElementsInDiagram failed: {ex}", file=sys.stderr)

        results.append({
            "diagram_name"   : name,
            "classes"        : classes,
            "objects"        : objects,
            "dependencies"   : dependencies,
            "generalizations": generalizations,
            "summary": {
                "total_classes"       : len(classes),
                "total_operations"    : sum(c["summary"]["total_operations"] for c in classes),
                "total_attributes"    : sum(c["summary"]["total_attributes"] for c in classes),
                "total_dependencies"  : len(dependencies),
                "total_generalizations": len(generalizations),
            }
        })

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--diagram",   default=None)
    parser.add_argument("--output",    default=None)
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

    bdds = read_bdd_from_package(dd_pkg, diagram_name=args.diagram)
    result = {"component": args.component, "bdds": bdds}

    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"[ReadBDD] Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)
