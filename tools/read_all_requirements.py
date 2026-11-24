"""
read_all_requirements.py
-------------------------
Reads ALL requirements for a component from the AB12Requirements::SRS
subtree, recursively, skipping headings and other non-requirement elements.

Since State tags are not populated in this project, ALL non-heading
requirements are returned (filtering by "approved" state is not applicable
when the state field is unset/default for all requirements).

Output format:
{
  "component": "rb_sdm_SafeDataMgt",
  "srs_package": "SRS_SDM_SafeDataMgt",
  "requirements": [
    {
      "id":          "SRS_SDM_217",
      "guid":        "GUID ...",
      "description": "The component shall ...",
      "stereotypes": ["fromAB12SRSElement", "AB12SRSElement"],
      "state":       "---",
      "path":        "SRS_SDM_SafeDataMgt::SRS_SDM_217"
    },
    ...
  ],
  "count": 42
}

Usage:
    python read_all_requirements.py --component rb_sdm_SafeDataMgt
    python read_all_requirements.py --component rb_sdm_SafeDataMgt --state approved
"""
import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
import sys
import os
import json
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_rhapsody():
    rhapsody = rhapsody_connect.get_active_rhapsody()
    project  = rhapsody.activeProject()
    if not project:
        raise RuntimeError("No active project")
    return rhapsody, project


HEADING_STEREOTYPES = {
    "AB12HeadingRequirement",
    "AB12Section",
    "AB12Chapter",
}

# Confirmed skip list from probe (117 total, 81 real requirements):
# AB12HeadingRequirement (30) — section headings, not requirements
# AB12Definition (6) — definitions, not requirements
# Everything else is a real requirement:
#   AB12FunctionalRequirement (17)
#   AB12NonFunctionalRequirement (26)
#   AB12ComponentRequirement (18)
#   AB12CommentRequirement (16)
#   AB12UseCaseRequirement (4)
SKIP_STEREOTYPES = HEADING_STEREOTYPES | {
    "AB12Definition",
}

# What we keep — for documentation / future filtering
REQUIREMENT_STEREOTYPES = {
    "AB12FunctionalRequirement",
    "AB12NonFunctionalRequirement",
    "AB12ComponentRequirement",
    "AB12CommentRequirement",
    "AB12UseCaseRequirement",
}


def get_req_state(req_elem):
    """Read the State tag value. Returns '---' if unset."""
    try:
        tag = req_elem.getTag("State")
        return str(tag.value or "---").strip()
    except:
        return "---"


def get_req_description(req_elem):
    """Read requirement text from specification property."""
    for attr in ["specification", "body", "description"]:
        try:
            val = getattr(req_elem, attr)
            if callable(val): val = val()
            if val and str(val).strip():
                return str(val).strip()
        except:
            pass
    return ""


def get_req_stereotypes(req_elem):
    try:
        sts = req_elem.stereotypes
        return [sts.Item(i).name for i in range(1, sts.Count + 1)]
    except:
        return []


def collect_requirements_recursive(pkg, results, filter_state=None, depth=0, max_depth=20):
    """
    Walk the package tree recursively, collecting all Requirement elements
    that are not headings/definitions. Uses getNestedElementsRecursive()
    on each sub-package for efficiency (same pattern as find_requirement.py,
    confirmed ~0.01s even on 1290 nested elements).
    """
    if depth > max_depth:
        return

    try:
        nested = pkg.getNestedElementsRecursive()
        for i in range(1, nested.Count + 1):
            e = nested.Item(i)
            try:
                if e.metaClass != "Requirement":
                    continue

                sts_names = get_req_stereotypes(e)

                # Skip headings, sections, definitions
                if any(s in SKIP_STEREOTYPES for s in sts_names):
                    continue

                state = get_req_state(e)

                # Apply state filter if requested
                if filter_state and state.lower() != filter_state.lower():
                    continue

                try:
                    guid = str(e.GUID)
                except:
                    guid = ""

                try:
                    path = e.getFullPathName()
                except:
                    path = e.name

                # Derive requirement type from stereotype
                req_type = "requirement"
                for st in sts_names:
                    if st.startswith("AB12") and st.endswith("Requirement") \
                            and st != "AB12HeadingRequirement":
                        req_type = st.replace("AB12", "").replace("Requirement", "").lower()
                        break

                results.append({
                    "id"          : e.name,
                    "guid"        : guid,
                    "text"        : get_req_description(e),
                    "type"        : req_type,
                    "state"       : state,
                    "path"        : path,
                })
            except:
                pass
    except Exception as ex:
        print(f"[ReadAllReq] getNestedElementsRecursive failed on "
              f"{pkg.name}: {ex}", file=sys.stderr)


def _component_short_name(component_name: str) -> str:
    # "rb_sdm_SafeDataMgt" -> "SafeDataMgt" (SRS folders are named SRS_SDM_SafeDataMgt)
    parts = component_name.split("_")
    return parts[2] if len(parts) >= 3 else (parts[-1] if parts else component_name)


# Explicit SRS package name(s) per component (set when the user gives "srs=...")
_SRS_OVERRIDE: dict = {}


def _norm_srs_names(srs_names) -> list:
    if not srs_names:
        return []
    if isinstance(srs_names, str):
        srs_names = srs_names.split(",")
    # accept full paths like "AB12Requirements::SRS::SRS_SDM_X"
    return [str(n).strip().strip('"').split("::")[-1].strip() for n in srs_names if str(n).strip()]


def set_srs_names(component_name: str, srs_names) -> None:
    """Remember explicit SRS package names for a component (used by every lookup
    in this process, e.g. requirement links of AD actions)."""
    names = _norm_srs_names(srs_names)
    if names:
        _SRS_OVERRIDE[component_name] = names
    else:
        _SRS_OVERRIDE.pop(component_name, None)


def _srs_roots(project):
    """[(top package, SRS package)] for every top-level package with an 'SRS' package."""
    roots = []
    try:
        tops = [project.packages.Item(i) for i in range(1, project.packages.Count + 1)]
    except Exception as e:
        print(f"[ReadAllReq] Cannot search packages: {e}", file=sys.stderr)
        return roots
    for top in tops:
        try:
            for j in range(1, top.packages.Count + 1):
                if top.packages.Item(j).name == "SRS":
                    roots.append((top, top.packages.Item(j)))
        except Exception:
            pass
    return roots


def list_srs_packages(project) -> list:
    """Names of all SRS sub-packages (for error messages)."""
    names = []
    for top, srs in _srs_roots(project):
        try:
            names += [f"{top.name}::SRS::{srs.packages.Item(k).name}"
                      for k in range(1, srs.packages.Count + 1)]
        except Exception:
            pass
    return names


def find_srs_packages(project, component_name, srs_names=None):
    """ALL SRS sub-packages of a component, in EVERY top-level package that has
    an 'SRS' package (AB12Requirements, AB12Requirements_v2, CustRequirements, ...).

    srs_names : explicit SRS package name(s) (when they differ from the component
                name) — exact name match first (case-insensitive), else "contains".
                Defaults to the names set with set_srs_names() for the component.
    Returns [(package, "Top::SRS::Sub"), ...]."""
    wanted = _norm_srs_names(srs_names) or _SRS_OVERRIDE.get(component_name, [])
    if wanted:
        return _find_named_srs(project, wanted)
    short = _component_short_name(component_name).lower()
    found, seen = [], set()
    try:
        tops = [project.packages.Item(i) for i in range(1, project.packages.Count + 1)]
    except Exception as e:
        print(f"[ReadAllReq] Cannot search packages: {e}", file=sys.stderr)
        return []
    for top in tops:
        try:
            srs_list = [top.packages.Item(j) for j in range(1, top.packages.Count + 1)
                        if top.packages.Item(j).name == "SRS"]
        except Exception:
            continue
        for srs in srs_list:
            try:
                subs = [srs.packages.Item(k) for k in range(1, srs.packages.Count + 1)]
            except Exception:
                continue
            for sub in subs:
                try:
                    if short not in sub.name.lower():
                        continue
                    key = str(sub.GUID) if getattr(sub, "GUID", None) else f"{top.name}::{sub.name}"
                except Exception:
                    key = f"{top.name}::{sub.name}"
                if key in seen:
                    continue
                seen.add(key)
                found.append((sub, f"{top.name}::SRS::{sub.name}"))
    if not found:
        print(f"[ReadAllReq] No SRS sub-package matching '{short}' in any requirements package. "
              f"Available: {list_srs_packages(project)}", file=sys.stderr)
    else:
        print(f"[ReadAllReq] SRS packages for {component_name}: {[l for _, l in found]}",
              file=sys.stderr)
    return found


def _find_named_srs(project, wanted: list) -> list:
    subs = []
    for top, srs in _srs_roots(project):
        try:
            subs += [(srs.packages.Item(k), f"{top.name}::SRS::{srs.packages.Item(k).name}")
                     for k in range(1, srs.packages.Count + 1)]
        except Exception:
            pass
    found, seen = [], set()
    for name in wanted:
        exact = [x for x in subs if x[0].name.lower() == name.lower()]
        hits = exact or [x for x in subs if name.lower() in x[0].name.lower()]
        if not hits:
            print(f"[ReadAllReq] SRS package '{name}' not found", file=sys.stderr)
        for pkg, label in hits:
            if label not in seen:
                seen.add(label)
                found.append((pkg, label))
    print(f"[ReadAllReq] SRS packages (given {wanted}): {[l for _, l in found]}", file=sys.stderr)
    return found


def find_srs_package(project, component_name):
    """Backward compatible: the first matching SRS package (prefer find_srs_packages)."""
    pkgs = find_srs_packages(project, component_name)
    return (pkgs[0][0], pkgs[0][1]) if pkgs else (None, None)


def collect_all_requirements(project, component_name, filter_state=None, srs_names=None):
    """Requirements from ALL SRS packages of the component (or of the given SRS
    package names), de-duplicated. Returns (requirements, [package labels])."""
    pkgs = find_srs_packages(project, component_name, srs_names)
    reqs, by_guid, by_id = [], set(), {}
    for pkg, label in pkgs:
        part = []
        collect_requirements_recursive(pkg, part, filter_state=filter_state)
        for r in part:
            if r.get("guid") and r["guid"] in by_guid:
                continue            # same element reached twice
            if r["id"] in by_id:
                print(f"[ReadAllReq] Requirement {r['id']} exists in {by_id[r['id']]} and "
                      f"{label} - keeping the first", file=sys.stderr)
                continue
            if r.get("guid"):
                by_guid.add(r["guid"])
            by_id[r["id"]] = label
            r["srs_package"] = label
            reqs.append(r)
    return reqs, [l for _, l in pkgs]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--state", default=None,
                        help="Filter by state value (e.g. 'approved'). "
                             "Omit to return all requirements.")
    parser.add_argument("--output", default=None)
    parser.add_argument("--srs", default=None,
                        help="SRS package name(s) when they differ from the component name")
    args = parser.parse_args()

    _, project = get_rhapsody()

    requirements, srs_names = collect_all_requirements(project, args.component,
                                                       filter_state=args.state, srs_names=args.srs)
    srs_pkg_name = ", ".join(srs_names)
    if not srs_names:
        print(json.dumps({
            "component": args.component,
            "error": "SRS package not found",
            "requirements": [],
            "count": 0,
        }))
        sys.exit(1)


    print(f"[ReadAllReq] Found {len(requirements)} requirement(s)", file=sys.stderr)

    result = {
        "component"  : args.component,
        "srs_package": srs_pkg_name,
        "requirements": requirements,
        "count"      : len(requirements),
    }

    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"[ReadAllReq] Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)
