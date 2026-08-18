"""
confirm_changes.py
------------------
Confirms or rejects LLM-generated changes by finding elements
tagged with 'LLMGenerated' in the diagrams.

Usage:
    python confirm_changes.py --component rb_sdm_SafeDataMgt
    python confirm_changes.py --component rb_sdm_SafeDataMgt --reject
"""
import sys
import os
import argparse
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhapsody_com import get_sw_model
from read_detailed_ad import find_package_recursive, find_dd_packages


def get_rhapsody():
    rhapsody = win32com.client.GetActiveObject("Rhapsody2.Application")
    return rhapsody, rhapsody.activeProject()


def has_llm_tag(elem):
    """Check if element has LLMGenerated tag."""
    try:
        tags = elem.tags
        for i in range(1, tags.Count + 1):
            if tags.Item(i).name == "LLMGenerated":
                return True
    except: pass
    return False


def remove_llm_tag(elem):
    """Remove LLMGenerated tag from element."""
    try:
        tags = elem.tags
        for i in range(1, tags.Count + 1):
            t = tags.Item(i)
            if t.name == "LLMGenerated":
                t.deleteFromProject()
                return True
    except: pass
    return False


def reset_highlight(diag, elem):
    """Reset yellow highlight to default."""
    try:
        ges = diag.getCorrespondingGraphicElements(elem)
        for j in range(1, ges.Count + 1):
            ges.Item(j).setGraphicalProperty("FillColor", "")
    except: pass


def confirm_changes(component_name: str, reject: bool = False):
    rhapsody, project = get_rhapsody()
    sw = get_sw_model(project) or project
    comp_pkg = find_package_recursive(sw, component_name)
    dd_pkgs  = find_dd_packages(comp_pkg)
    dd_pkg   = next((x for x in dd_pkgs if "Cfg" not in x.name), None)

    if not dd_pkg:
        print("ERROR: DD package not found")
        return

    action = "Rejecting" if reject else "Confirming"
    print(f"{action} LLM changes for {component_name}...")
    confirmed = []
    rejected  = []

    # ── IBD ───────────────────────────────────────────────────────────────────
    try:
        ibd = dd_pkg.structureDiagrams.Item(1)
        print(f"\n[IBD] {ibd.name}")
        elems = ibd.getElementsInDiagram()
        to_delete = []
        for i in range(1, elems.Count + 1):
            e = elems.Item(i)
            try:
                if has_llm_tag(e):
                    name = e.name
                    if reject:
                        to_delete.append(e)
                        rejected.append(name)
                    else:
                        reset_highlight(ibd, e)
                        remove_llm_tag(e)
                        confirmed.append(name)
                        print(f"  ✅ Confirmed: {name}")
            except: pass
        for e in to_delete:
            try:
                print(f"  ❌ Deleting: {e.name}")
                e.deleteFromProject()
            except Exception as ex:
                print(f"  Delete failed: {ex}")
    except Exception as ex:
        print(f"[IBD] {ex}")

    # ── BDDs ──────────────────────────────────────────────────────────────────
    try:
        for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
            bdd = dd_pkg.objectModelDiagrams.Item(i)
            print(f"\n[BDD] {bdd.name}")
            elems = bdd.getElementsInDiagram()
            to_delete = []
            for j in range(1, elems.Count + 1):
                e = elems.Item(j)
                try:
                    if has_llm_tag(e):
                        name = e.name
                        if reject:
                            to_delete.append(e)
                            rejected.append(name)
                        else:
                            reset_highlight(bdd, e)
                            remove_llm_tag(e)
                            confirmed.append(name)
                            print(f"  ✅ Confirmed: {name}")
                except: pass
            for e in to_delete:
                try:
                    print(f"  ❌ Deleting: {e.name}")
                    e.deleteFromProject()
                except Exception as ex:
                    print(f"  Delete failed: {ex}")
    except Exception as ex:
        print(f"[BDD] {ex}")

    try:
        project.save()
        print("\nSaved ✅")
    except Exception as e:
        print(f"Save failed: {e}")

    if confirmed:
        print(f"\nConfirmed {len(confirmed)} elements: {confirmed}")
    if rejected:
        print(f"\nRejected {len(rejected)} elements: {rejected}")
    if not confirmed and not rejected:
        print("\nNo LLM-generated elements found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    confirm_changes(args.component, reject=args.reject)
