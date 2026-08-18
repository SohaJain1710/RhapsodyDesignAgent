"""
read_detailed_ad.py
--------------------
Reads an existing Detailed (use-case-level, code-ready) Activity Diagram
into structured JSON: actions in flow order, their text, guards/decisions,
and each action's linked SRS requirements.

This is the reference-data tool for the surgical diff-and-patch workflow:
before proposing any change to a Detailed AD, we need to know exactly what
it currently contains.

Output format:
{
  "diagram_name": "LoadSdmDataAD",
  "use_case": "Load sdm data",
  "module": "rb_sdm_SafeDataMgt",
  "actions": [
    {
      "name": "action_0",
      "text": "1. Check NVM section is configured...",
      "stereotypes": ["Analysis", "Allocated"],
      "requirements": ["SRS_SDM_200", "SRS_SDM_198", ...]
    },
    ...
  ],
  "decisions": [{"name": "decision"}],
  "merges": [{"name": "mergenode_98"}],
  "transitions": [
    {"from": "ROOT", "to": "action_0", "guard": null, "type": "DefaultTransition"},
    {"from": "action_0", "to": "decision", "guard": null, "type": "Transition"},
    {"from": "decision", "to": "action_12", "guard": "[condition text]", "type": "Transition"},
    ...
  ],
  "summary": {
    "total_actions": 5,
    "total_requirements_linked": 27,
    "actions_without_requirements": []
  }
}

Usage:
    python read_detailed_ad.py --component rb_sdm_SafeDataMgt --diagram LoadSdmDataAD
"""
import sys
import os
import json
import argparse
import win32com.client


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    project  = rhapsody.activeProject()
    if not project:
        raise RuntimeError("No active project")
    return rhapsody, project


def find_package_recursive(parent, name, depth=0, max_depth=12):
    if depth > max_depth:
        return None
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            if pkg.name == name:
                return pkg
            r = find_package_recursive(pkg, name, depth + 1)
            if r:
                return r
    except:
        pass
    return None


def find_dd_packages(parent, keyword="DetailedDesign", depth=0, max_depth=8, results=None):
    if results is None:
        results = []
    if depth > max_depth:
        return results
    try:
        for i in range(1, parent.packages.Count + 1):
            pkg = parent.packages.Item(i)
            if keyword in pkg.name:
                results.append(pkg)
            find_dd_packages(pkg, keyword, depth+1, max_depth, results)
    except:
        pass
    return results


def find_behavioral_diagram(dd_pkgs, diagram_name):
    for dd_pkg in dd_pkgs:
        try:
            for i in range(1, dd_pkg.behavioralDiagrams.Count + 1):
                d = dd_pkg.behavioralDiagrams.Item(i)
                if d.name == diagram_name:
                    return d, dd_pkg
        except:
            pass
    return None, None


def _get_guid(elem):
    """Safe GUID read — returns full GUID string or empty string."""
    try:
        return str(elem.GUID)
    except:
        return ""


def _get_stereos(elem):
    try:
        sts = elem.stereotypes
        return [sts.Item(k).name for k in range(1, sts.Count + 1)]
    except:
        return []


def read_detailed_ad(diagram):
    """
    Full structured read of a Detailed Activity Diagram.

    Captures:
      - Actions (name, guid, entryAction, exitAction, stereotypes, requirements)
      - Initial node (stateType=Or/ROOT)
      - Final node (stateType=LocalTermination)
      - Decisions / Conditions (name, guid)
      - Junctions (name, guid, connectorType for fork/join/merge)
      - Transitions (from/to names + GUIDs, guard, type)
      - Swimlanes / ActivityPartition (name, guid, covered elements)
      - Diagram-level GUID, use_case, module
    """
    elems = diagram.getElementsInDiagram()

    diagram_guid = _get_guid(diagram)
    actions      = []
    decisions    = []
    junctions    = []   # merges, forks, joins
    transitions  = []
    swimlanes    = []
    initial_node = None
    final_nodes  = []
    use_case     = None
    module       = None

    # Build guid→name map for transition endpoint resolution
    guid_to_name = {}

    # First pass: collect all elements and build guid→name map
    all_elems = []
    for j in range(1, elems.Count + 1):
        e = elems.Item(j)
        try:
            mc = e.metaClass
            guid = _get_guid(e)
            try:
                name = e.name
            except:
                name = ""
            if guid and name:
                guid_to_name[guid] = name
            all_elems.append((mc, e, guid, name))
        except:
            pass

    # Second pass: structured extraction
    for mc, e, guid, name in all_elems:

        if mc == "UseCase":
            use_case = name

        elif mc == "Class":
            module = name

        elif mc == "State":
            try:
                state_type = str(e.stateType)
            except:
                state_type = ""

            if state_type == "Action":
                entry = ""
                try: entry = e.entryAction or ""
                except: pass
                exit_act = ""
                try: exit_act = e.exitAction or ""
                except: pass
                reqs = []
                try:
                    deps = e.dependencies
                    for k in range(1, deps.Count + 1):
                        reqs.append(deps.Item(k).name)
                except: pass
                allocated_module = ""
                try:
                    tag = e.getTag("AllocatedToModule")
                    allocated_module = str(tag.value or "").strip()
                except: pass
                actions.append({
                    "name"           : name,
                    "guid"           : guid,
                    "entry_action"   : entry,
                    "exit_action"    : exit_act,
                    "stereotypes"    : _get_stereos(e),
                    "requirements"   : reqs,
                    "allocated_module": allocated_module,
                })

            elif state_type in ("Or", "And"):
                # Root composite state = initial pseudostate
                initial_node = {"name": name, "guid": guid}

            elif state_type == "LocalTermination":
                final_nodes.append({"name": name, "guid": guid})

        elif mc == "Condition":
            decisions.append({
                "name": name,
                "guid": guid,
            })

        elif mc == "JunctionConnector":
            connector_type = ""
            try:
                connector_type = str(e.connectorType or "")
            except: pass
            junctions.append({
                "name"          : name,
                "guid"          : guid,
                "connector_type": connector_type,  # Junction/Fork/Join
            })

        elif mc in ("ActivityPartition", "Swimlane"):
            covered = []
            try:
                for k in range(1, e.coveredElements.Count + 1):
                    covered.append(e.coveredElements.Item(k).name)
            except: pass
            swimlanes.append({
                "name"   : name,
                "guid"   : guid,
                "covered": covered,
            })

        elif mc == "Transition":
            try:
                src = e.itsSource
                tgt = e.itsTarget
                src_name = src.name if src else None
                tgt_name = tgt.name if tgt else None
                src_guid = _get_guid(src) if src else ""
                tgt_guid = _get_guid(tgt) if tgt else ""
                guard = None
                try:
                    g = e.getItsGuard()
                    if g: guard = g.body
                except: pass
                transitions.append({
                    "from"     : src_name,
                    "from_guid": src_guid,
                    "to"       : tgt_name,
                    "to_guid"  : tgt_guid,
                    "guard"    : guard,
                    "guid"     : guid,
                    "type"     : "Transition",
                })
            except Exception as ex:
                print(f"[ReadDetailedAD] Transition read failed: {ex}", file=sys.stderr)

        elif mc == "DefaultTransition":
            try:
                tgt = e.itsTarget
                tgt_name = tgt.name if tgt else None
                tgt_guid = _get_guid(tgt) if tgt else ""
                transitions.append({
                    "from"     : "initial",
                    "from_guid": initial_node["guid"] if initial_node else "",
                    "to"       : tgt_name,
                    "to_guid"  : tgt_guid,
                    "guard"    : None,
                    "guid"     : guid,
                    "type"     : "DefaultTransition",
                })
            except Exception as ex:
                print(f"[ReadDetailedAD] DefaultTransition read failed: {ex}", file=sys.stderr)

    all_reqs = set()
    for a in actions:
        all_reqs.update(a["requirements"])

    actions_without_requirements = [a["name"] for a in actions
                                     if not a["requirements"]]

    return {
        "diagram_name" : diagram.name,
        "diagram_guid" : diagram_guid,
        "use_case"     : use_case,
        "module"       : module,
        "initial_node" : initial_node,
        "final_nodes"  : final_nodes,
        "actions"      : actions,
        "decisions"    : decisions,
        "junctions"    : junctions,
        "swimlanes"    : swimlanes,
        "transitions"  : transitions,
        "summary": {
            "total_actions"               : len(actions),
            "total_decisions"             : len(decisions),
            "total_junctions"             : len(junctions),
            "total_swimlanes"             : len(swimlanes),
            "total_requirements_linked"   : len(all_reqs),
            "actions_without_requirements": actions_without_requirements,
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--diagram",   required=True)
    parser.add_argument("--output",    default=None)
    args = parser.parse_args()

    rhapsody, project = get_rhapsody()

    comp_pkg = find_package_recursive(project, args.component)
    if not comp_pkg:
        print(json.dumps({"error": f"Component '{args.component}' not found"}))
        sys.exit(1)

    dd_pkgs = find_dd_packages(comp_pkg)
    if not dd_pkgs:
        print(json.dumps({"error": "No DetailedDesign packages found"}))
        sys.exit(1)

    diagram, dd_pkg = find_behavioral_diagram(dd_pkgs, args.diagram)
    if not diagram:
        print(json.dumps({"error": f"Diagram '{args.diagram}' not found"}))
        sys.exit(1)

    result = read_detailed_ad(diagram)
    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"[ReadDetailedAD] Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)
