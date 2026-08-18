"""
create_operation_ad.py
----------------------
Creates an Activity Diagram for a specific operation in Rhapsody.

Usage:
    python create_operation_ad.py --component rb_sdm_SafeDataMgt
        --operation rb_sdm_Init
        --mermaid "C:\\RhapsodyAIAgent_runtime\\rb_sdm_Init_AD.mmd"

The Mermaid file should be a standard flowchart TD/LR:
    flowchart TD
        Start([Start])
        A[Initialize NVM]
        B{NVM ready?}
        C[Set fault flag]
        End([End])
        Start --> A
        A --> B
        B -->|Yes| End
        B -->|No| C
        C --> End
"""
import sys
import os
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhapsody_com import get_sw_model
from read_detailed_ad import find_package_recursive, find_dd_packages
from mermaid_to_ad import from_mermaid
from create_activity_diagram import create_activity_diagram


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    return rhapsody, rhapsody.activeProject()


def find_class_recursive(parent, name, depth=0):
    if depth > 8: return None
    try:
        for i in range(1, parent.classes.Count + 1):
            if parent.classes.Item(i).name == name:
                return parent.classes.Item(i)
    except: pass
    try:
        for i in range(1, parent.packages.Count + 1):
            r = find_class_recursive(parent.packages.Item(i), name, depth+1)
            if r: return r
    except: pass
    return None


def find_operation(cls, op_name):
    """Find an operation by name on a class."""
    try:
        ops = cls.operations
        for i in range(1, ops.Count + 1):
            if ops.Item(i).name == op_name:
                return ops.Item(i)
    except: pass
    return None


def get_design_stereotype(project):
    """Get the AB12DetailedDesign::Design stereotype."""
    DESIGN_GUID = "GUID 4fcb9b50-1008-4fff-8770-584342da54f9"
    try:
        nested = project.getNestedElementsByMetaClass("Stereotype", 1)
        for i in range(1, nested.Count + 1):
            st = nested.Item(i)
            try:
                if st.GUID == DESIGN_GUID:
                    return st
            except: pass
    except: pass
    return None


def create_operation_ad(component_name: str, op_name: str,
                        mermaid_str: str, rhapsody=None) -> dict:
    """
    Create an Activity Diagram for a specific operation.
    """
    if rhapsody is None:
        rhapsody, project = get_rhapsody()
    else:
        project = rhapsody.activeProject()

    sw = get_sw_model(project) or project
    comp_pkg = find_package_recursive(sw, component_name)
    if not comp_pkg:
        return {"success": False, "error": f"Component '{component_name}' not found"}

    dd_pkgs = find_dd_packages(comp_pkg)
    dd_pkg  = next((x for x in dd_pkgs if "Cfg" not in x.name), None)
    if not dd_pkg:
        return {"success": False, "error": "No DetailedDesign package found"}

    # Find module class
    module_cls = None
    for c in _walk_cls(dd_pkg):
        try:
            sts = [c.stereotypes.Item(i).name for i in range(1, c.stereotypes.Count+1)]
            if "AB12Module" in sts and c.name == component_name:
                module_cls = c; break
        except: pass

    if not module_cls:
        return {"success": False, "error": f"Module class not found for {component_name}"}

    # Find operation
    op = find_operation(module_cls, op_name)
    if not op:
        return {"success": False,
                "error": f"Operation '{op_name}' not found on {component_name}"}

    print(f"[OpAD] Found op: {op.name} on {module_cls.name}")

    # Check if AD already exists
    ad_name = f"{op_name}AD"
    try:
        nested = op.getNestedElementsRecursive()
        for i in range(1, nested.Count + 1):
            e = nested.Item(i)
            try:
                if e.metaClass == "ActivityDiagram":
                    print(f"[OpAD] AD already exists: {e.name}")
                    return {"success": False,
                            "error": f"AD already exists: {e.name}"}
            except: pass
    except: pass


    # Parse Mermaid to AD spec — support both 'flowchart TD' and 'graph TD'
    print(f"[OpAD] Parsing Mermaid...")
    normalized = mermaid_str.replace("flowchart TD", "graph TD")\
                            .replace("flowchart LR", "graph LR")
    spec = from_mermaid(normalized)
    print(f"[OpAD] Actions: {len(spec.get('actions', []))} "
          f"Transitions: {len(spec.get('transitions', []))}")

    # Create AD on the operation — op acts as parent like dd_pkg
    print(f"[OpAD] Creating ActivityDiagram on operation...")
    try:
        from create_activity_diagram import create_activity_diagram
        # Pass Design stereotype via conventions
        design_st = get_design_stereotype(project)
        conventions = {"action_stereotype": design_st} if design_st else {}
        result = create_activity_diagram(op, ad_name, spec, rhapsody, conventions)
        project.save()
        print(f"[OpAD] Done: {ad_name}")
        return {
            "success"   : result.get("success", False),
            "diagram"   : ad_name,
            "operation" : op_name,
            "component" : component_name,
            "errors"    : result.get("errors", []),
        }
    except Exception as e:
        return {"success": False, "error": f"create_activity_diagram failed: {e}"}


def _walk_cls(parent, depth=0):
    if depth > 8: return []
    result = []
    try:
        for i in range(1, parent.classes.Count + 1):
            result.append(parent.classes.Item(i))
    except: pass
    try:
        for i in range(1, parent.packages.Count + 1):
            result.extend(_walk_cls(parent.packages.Item(i), depth+1))
    except: pass
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--mermaid",   required=True,
                        help="Path to Mermaid flowchart file")
    args = parser.parse_args()

    with open(args.mermaid, encoding="utf-8-sig") as f:
        mermaid_str = f.read()

    import json
    rhapsody, _ = get_rhapsody()
    result = create_operation_ad(args.component, args.operation,
                                 mermaid_str, rhapsody)
    print(json.dumps(result, indent=2))
