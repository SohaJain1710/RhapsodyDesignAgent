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
import sys
import os
import json
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
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


def find_srs_package(project, component_name):
    """
    Find <RequirementsPackage>::SRS::SRS_<Component> package.
    Searches all top-level packages for one containing an SRS subtree.
    Works with any naming convention (AB12Requirements, CustRequirements, etc.)
    """
    # Search all top-level packages for one containing an SRS sub-package
    req_pkg_candidates = []
    try:
        for i in range(1, project.packages.Count + 1):
            pkg = project.packages.Item(i)
            try:
                for j in range(1, pkg.packages.Count + 1):
                    sub = pkg.packages.Item(j)
                    if sub.name == "SRS":
                        req_pkg_candidates.append((pkg, sub))
            except: pass
    except Exception as e:
        print(f"[ReadAllReq] Cannot search packages: {e}", file=sys.stderr)
        return None, None

    if not req_pkg_candidates:
        print("[ReadAllReq] No SRS package found under any top-level package", file=sys.stderr)
        return None, None

    ab12req, srs_pkg_parent = req_pkg_candidates[0]
    print(f"[ReadAllReq] Found requirements under: {ab12req.name}::SRS", file=sys.stderr)

    parts = component_name.split("_")
    short_name = parts[2] if len(parts) >= 3 else (parts[-1] if parts else component_name)

    try:
        for j in range(1, srs_pkg_parent.packages.Count + 1):
            comp_srs = srs_pkg_parent.packages.Item(j)
            if short_name.lower() in comp_srs.name.lower():
                return comp_srs, comp_srs.name
    except Exception as e:
        print(f"[ReadAllReq] SRS subtree search failed: {e}", file=sys.stderr)

    print(f"[ReadAllReq] No SRS subtree found matching '{short_name}' "
          f"under SRS package", file=sys.stderr)
    return None, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--state", default=None,
                        help="Filter by state value (e.g. 'approved'). "
                             "Omit to return all requirements.")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    _, project = get_rhapsody()

    srs_pkg, srs_pkg_name = find_srs_package(project, args.component)
    if not srs_pkg:
        print(json.dumps({
            "component": args.component,
            "error": "SRS package not found",
            "requirements": [],
            "count": 0,
        }))
        sys.exit(1)

    print(f"[ReadAllReq] Scanning {srs_pkg_name}...", file=sys.stderr)

    requirements = []
    collect_requirements_recursive(srs_pkg, requirements, filter_state=args.state)

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
