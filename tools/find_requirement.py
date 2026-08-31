"""
find_requirement.py
---------------------
Locates an SRS_SDM_xxx requirement element in the project-wide
AB12Requirements package tree, and finds which use case(s) (if any) it's
already linked to via the component's ReqLinkingUCD-style diagram.

This is the entry point for the surgical update workflow: given a
requirement ID the user provides, find where it lives and what it's
already connected to before diffing against any Detailed AD.

Output format:
{
  "requirement_id": "SRS_SDM_999",
  "found": true,
  "full_path": "AB12Requirements::SRS::SRS_SDM_SafeDataMgt::...::SRS_SDM_999",
  "stereotypes": ["fromAB12SRSElement", "AB12SRSElement", ...],
  "linked_use_cases": ["Load sdm data"],   # via existing Dependency, if any
  "already_in_detailed_ad": [
    {"diagram": "LoadSdmDataAD", "action": "action_0"}
  ]
}

If the requirement is brand new (not yet linked to any use case), 
linked_use_cases will be empty — the LLM patch step then needs the user
or the requirement text itself to indicate which use case it belongs to.

Usage:
    python find_requirement.py --component rb_sdm_SafeDataMgt --requirement SRS_SDM_999
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
    if not project:
        raise RuntimeError("No active project")
    return rhapsody, project


def find_requirement_in_ab12requirements(project, requirement_id, component_name=None):
    """
    Searches AB12Requirements::SRS::SRS_SDM_<Component>::... for the
    requirement by exact name match. If component_name is given, scopes
    the search to that component's subtree first (faster); falls back to
    a full AB12Requirements search if not found there.
    """
    ab12req = None
    try:
        for i in range(1, project.packages.Count + 1):
            pkg = project.packages.Item(i)
            if pkg.name == "AB12Requirements":
                ab12req = pkg
                break
    except Exception as e:
        print(f"[FindRequirement] Could not access AB12Requirements: {e}", file=sys.stderr)
        return None

    if not ab12req:
        print("[FindRequirement] AB12Requirements package not found", file=sys.stderr)
        return None

    def search(pkg, depth=0):
        """Requirement elements aren't stored in .classes — use the generic
        nested-elements traversal and filter by metaclass + name."""
        try:
            nested = pkg.getNestedElementsRecursive()
            for i in range(1, nested.Count + 1):
                e = nested.Item(i)
                try:
                    if e.metaclass == "Requirement" and e.name == requirement_id:
                        return e
                except:
                    pass
        except Exception as ex:
            print(f"[FindRequirement] getNestedElementsRecursive failed on "
                  f"{pkg.name}: {ex}", file=sys.stderr)
        return None

    # Scope to the component's SRS subtree — this is fast (confirmed ~0.01s
    # even on 1290 nested elements) and sufficient for all known cases.
    # Deliberately NOT falling back to a full AB12Requirements scan (which
    # also covers SYDS/SYRS/ARS/CRS, four more comparably-sized trees) —
    # that fallback was the actual source of multi-second-plus hangs.
    if not component_name:
        print("[FindRequirement] No component given — cannot scope search; "
              "pass --component to locate the requirement", file=sys.stderr)
        return None

    try:
        srs_pkg = None
        # SRS folders are named "SRS_SDM_SafeDataMgt" style — using the
        # component's SHORT name (the part after the second underscore,
        # e.g. "rb_sdm_SafeDataMgt" -> "SafeDataMgt"), not the full
        # component package name, which never appears in the SRS folder name.
        parts = component_name.split("_")
        short_name = parts[2] if len(parts) >= 3 else (parts[-1] if parts else component_name)

        for i in range(1, ab12req.packages.Count + 1):
            sub = ab12req.packages.Item(i)
            if sub.name == "SRS":
                for j in range(1, sub.packages.Count + 1):
                    comp_srs = sub.packages.Item(j)
                    if short_name.lower() in comp_srs.name.lower():
                        srs_pkg = comp_srs
                        break
            if srs_pkg:
                break
        if not srs_pkg:
            print(f"[FindRequirement] No SRS subtree found matching "
                  f"'{short_name}' (derived from '{component_name}') "
                  f"under AB12Requirements::SRS", file=sys.stderr)
            return None
        return search(srs_pkg)
    except Exception as e:
        print(f"[FindRequirement] Scoped search failed: {e}", file=sys.stderr)
        return None


def find_linked_use_cases(comp_pkg, requirement_element):
    """
    Searches the component's ReqLinking UseCaseDiagram for a Dependency
    whose dependsOn matches this requirement, returning the use case
    name(s) it's linked to.
    """
    linked = []
    ucd = None
    try:
        for i in range(1, comp_pkg.useCaseDiagrams.Count + 1):
            d = comp_pkg.useCaseDiagrams.Item(i)
            if "ReqLinking" in d.name:
                ucd = d
                break
    except:
        pass

    if not ucd:
        return linked

    try:
        elems = ucd.getElementsInDiagram()
        for j in range(1, elems.Count + 1):
            e = elems.Item(j)
            try:
                if e.metaclass == "Dependency":
                    if e.dependsOn.name == requirement_element.name:
                        linked.append(e.dependent.name)
            except:
                pass
    except Exception as ex:
        print(f"[FindRequirement] ReqLinking scan failed: {ex}", file=sys.stderr)

    return linked


def find_in_detailed_ads(dd_pkgs, requirement_element):
    """
    Searches all behavioral diagrams (Detailed/Analysis ADs) in the
    component's DetailedDesign packages for an action already linked to
    this requirement via action.dependencies.
    """
    found = []
    for dd_pkg in dd_pkgs:
        try:
            for i in range(1, dd_pkg.behavioralDiagrams.Count + 1):
                d = dd_pkg.behavioralDiagrams.Item(i)
                try:
                    elems = d.getElementsInDiagram()
                except:
                    continue
                for j in range(1, elems.Count + 1):
                    e = elems.Item(j)
                    try:
                        if e.metaclass != "State":
                            continue
                        if str(e.stateType) != "Action":
                            continue
                        deps = e.dependencies
                        for k in range(1, deps.Count + 1):
                            if deps.Item(k).name == requirement_element.name:
                                found.append({"diagram": d.name, "action": e.name})
                    except:
                        pass
        except:
            pass
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component",   required=True)
    parser.add_argument("--requirement", required=True)
    parser.add_argument("--output",      default=None)
    args = parser.parse_args()

    rhapsody, project = get_rhapsody()

    req_elem = find_requirement_in_ab12requirements(
        project, args.requirement, component_name=args.component)

    if not req_elem:
        result = {"requirement_id": args.requirement, "found": False}
    else:
        try:
            full_path = req_elem.getFullPathName()
        except:
            full_path = ""
        try:
            sts = req_elem.stereotypes
            stereotypes = [sts.Item(k).name for k in range(1, sts.Count + 1)]
        except:
            stereotypes = []

        comp_pkg = find_package_recursive(project, args.component)
        linked_use_cases = find_linked_use_cases(comp_pkg, req_elem) if comp_pkg else []

        dd_pkgs = find_dd_packages(comp_pkg) if comp_pkg else []
        already_in_ad = find_in_detailed_ads(dd_pkgs, req_elem)

        result = {
            "requirement_id"        : args.requirement,
            "found"                 : True,
            "full_path"             : full_path,
            "stereotypes"           : stereotypes,
            "linked_use_cases"      : linked_use_cases,
            "already_in_detailed_ad": already_in_ad,
        }

    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"[FindRequirement] Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)
