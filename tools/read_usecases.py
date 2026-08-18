"""
read_usecases.py
-----------------
Read all use cases defined under a component package in Rhapsody.
Outputs: use case name, owning package, linked diagrams, and linked
requirements (if any).

Usage:
    python read_usecases.py --component rb_sdm_SafeDataMgt
"""
import sys
import os
import json
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhapsody_com import get_rhapsody, get_sw_model


def find_package_recursive(parent, name, depth=0, max_depth=12):
    if depth > max_depth:
        return None
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            if pkg.name == name:
                return pkg
            result = find_package_recursive(pkg, name, depth=depth + 1)
            if result:
                return result
    except:
        pass
    return None


def collect_usecases(parent, depth=0, max_depth=12):
    """Walk all packages recursively, collecting use cases."""
    usecases = []
    if depth > max_depth:
        return usecases

    # Use cases directly on this package
    try:
        for i in range(1, parent.useCases.Count + 1):
            uc = parent.useCases.Item(i)
            uc_data = {
                "name": uc.name,
                "owner": parent.name,
            }

            # GUID
            try:
                uc_data["guid"] = str(uc.GUID)
            except:
                pass

            # Description
            try:
                desc = uc.description
                if desc:
                    uc_data["description"] = desc[:200000]
            except:
                pass

            # Linked diagrams (dependencies from diagrams to this use case)
            linked_diagrams = []
            try:
                deps = uc.dependents
                for j in range(1, deps.Count + 1):
                    dep = deps.Item(j)
                    try:
                        if "Diagram" in dep.metaclass or "Activity" in dep.metaclass:
                            linked_diagrams.append(dep.name)
                    except:
                        pass
            except:
                pass

            # Also check hyperlinks / relations
            try:
                rels = uc.relations
                for j in range(1, rels.Count + 1):
                    rel = rels.Item(j)
                    try:
                        target = rel.dependsOn if hasattr(rel, 'dependsOn') else None
                        if target and "Diagram" in target.metaclass:
                            linked_diagrams.append(target.name)
                    except:
                        pass
            except:
                pass
            uc_data["linked_diagrams"] = linked_diagrams

            # Linked requirements
            linked_reqs = []
            try:
                deps = uc.dependencies
                for j in range(1, deps.Count + 1):
                    dep = deps.Item(j)
                    try:
                        if dep.metaclass == "Requirement":
                            linked_reqs.append(dep.name)
                    except:
                        pass
            except:
                pass
            uc_data["linked_requirements"] = linked_reqs

            # Stereotype
            try:
                sts = uc.stereotypes
                if sts.Count > 0:
                    uc_data["stereotypes"] = [sts.Item(k).name
                                              for k in range(1, sts.Count + 1)]
            except:
                pass

            usecases.append(uc_data)
    except:
        pass

    # Recurse into sub-packages
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            usecases.extend(collect_usecases(pkg, depth + 1, max_depth))
    except:
        pass

    # Also check classes (use cases can be owned by actors/classes)
    try:
        for i in range(1, parent.classes.Count + 1):
            cls = parent.classes.Item(i)
            try:
                for j in range(1, cls.useCases.Count + 1):
                    uc = cls.useCases.Item(j)
                    usecases.append({
                        "name": uc.name,
                        "owner": f"{parent.name}::{cls.name}",
                        "guid": str(uc.GUID) if hasattr(uc, 'GUID') else None,
                    })
            except:
                pass
    except:
        pass

    return usecases


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read all use cases in a component")
    parser.add_argument("--component", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    rhapsody, project = get_rhapsody()
    sw = get_sw_model(project) or project

    comp_pkg = find_package_recursive(sw, args.component)
    if not comp_pkg:
        result = {"error": f"Component '{args.component}' not found", "usecases": []}
        print(json.dumps(result, indent=2))
        sys.exit(1)

    print(f"[ReadUC] Scanning use cases under {args.component}...", file=sys.stderr)
    usecases = collect_usecases(comp_pkg)
    print(f"[ReadUC] Found {len(usecases)} use case(s)", file=sys.stderr)

    result = {
        "component": args.component,
        "usecases": usecases,
        "count": len(usecases),
    }

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[ReadUC] Saved to {args.output}", file=sys.stderr)
    else:
        print(output)
