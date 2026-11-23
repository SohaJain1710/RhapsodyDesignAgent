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
    if not project:
        raise RuntimeError("No active project")
    return rhapsody, project


_req_index_cache: dict = {}


def _requirement_index(project, component_name):
    """{requirement name: element} over ALL SRS packages of the component
    (every requirements package, every matching SRS sub-package), built once."""
    from read_all_requirements import find_srs_packages
    try:
        key = (str(project.GUID), component_name)
    except Exception:
        key = (id(project), component_name)
    if key in _req_index_cache:
        return _req_index_cache[key]
    index = {}
    for pkg, label in find_srs_packages(project, component_name):
        try:
            nested = pkg.getNestedElementsRecursive()
            for i in range(1, nested.Count + 1):
                e = nested.Item(i)
                try:
                    mc = getattr(e, "metaClass", None) or getattr(e, "metaclass", None)
                    if mc == "Requirement" and e.name not in index:
                        index[e.name] = e
                except Exception:
                    pass
        except Exception as ex:
            print(f"[FindRequirement] getNestedElementsRecursive failed on {label}: {ex}",
                  file=sys.stderr)
    _req_index_cache[key] = index
    return index


def find_requirement_in_ab12requirements(project, requirement_id, component_name=None):
    """
    Find a requirement by exact name in the component's SRS packages — ALL of
    them (any requirements package with an SRS tree, every matching SRS
    sub-package), not only the first one / only 'AB12Requirements'.
    Scoped to the component (a full scan of every requirements tree is slow).
    """
    if not component_name:
        print("[FindRequirement] No component given — cannot scope search; "
              "pass --component to locate the requirement", file=sys.stderr)
        return None
    elem = _requirement_index(project, component_name).get(requirement_id)
    if elem is None:
        print(f"[FindRequirement] {requirement_id} not found in the SRS packages of "
              f"{component_name}", file=sys.stderr)
    return elem


def find_linked_use_cases(comp_pkg, requirement_element):
    """
    Searches ALL of the component's 'ReqLinking' use case diagrams for a
    Dependency whose dependsOn matches this requirement, returning the use case
    name(s) it is linked to (previously only the first such diagram was read).
    """
    linked = []
    try:
        ucds = [comp_pkg.useCaseDiagrams.Item(i) for i in range(1, comp_pkg.useCaseDiagrams.Count + 1)]
    except Exception:
        ucds = []
    for ucd in (d for d in ucds if "ReqLinking" in d.name):
        try:
            elems = ucd.getElementsInDiagram()
            for j in range(1, elems.Count + 1):
                e = elems.Item(j)
                try:
                    mc = getattr(e, "metaClass", None) or getattr(e, "metaclass", None)
                    if mc == "Dependency" and e.dependsOn.name == requirement_element.name:
                        if e.dependent.name not in linked:
                            linked.append(e.dependent.name)
                except Exception:
                    pass
        except Exception as ex:
            print(f"[FindRequirement] ReqLinking scan failed on {ucd.name}: {ex}", file=sys.stderr)
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

    if req_elem is None:
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
