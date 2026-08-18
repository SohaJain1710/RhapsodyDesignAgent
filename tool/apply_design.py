"""
apply_design.py
---------------
Standalone apply function — used when graph checkpoint is consumed
but we still need to apply the design state to Rhapsody.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def apply_design_state(state: dict, component_name: str) -> dict:
    """Apply new operations, interfaces and IBD changes to Rhapsody."""
    errors = []
    op_ads = []
    print(f"[ApplyDirect] Applying for: {component_name}", file=sys.stderr)

    try:
        import win32com.client
        from create_bdd import create_bdd_from_plan
        from create_ibd import create_ibd

        r = win32com.client.GetActiveObject("Rhapsody2.Application")

        # New operations
        new_ops = state.get("new_operations", [])
        if new_ops:
            print(f"[ApplyDirect] Adding {len(new_ops)} operations", file=sys.stderr)
            ops_plan = {
                "component_name" : component_name,
                "classes"        : [{
                    "name"       : component_name,
                    "stereotypes": [],
                    "operations" : new_ops,
                    "attributes" : [],
                }],
                "realizations"   : [],
                "generalizations": [],
            }
            res = create_bdd_from_plan(ops_plan, r,
                                       diagram_name=state.get("bdd_dd_name","DetailedDesignBDD"))
            print(f"[ApplyDirect] BDD ops result: {res}", file=sys.stderr)
            if res.get("errors"):
                errors.extend(res["errors"])

        # New interfaces/classes
        bdd = state.get("bdd_delta", {})
        if bdd.get("classes") or bdd.get("realizations"):
            plan = {
                "component_name" : component_name,
                "classes"        : bdd.get("classes", []),
                "realizations"   : bdd.get("realizations", []),
                "generalizations": bdd.get("generalizations", []),
            }
            res = create_bdd_from_plan(plan, r,
                                       diagram_name=state.get("bdd_intf_name","DetailedDesignInterfacesBDD"))
            if res.get("errors"):
                errors.extend(res["errors"])

        # IBD delta
        ibd = state.get("ibd_delta", {})
        if ibd.get("ports"):
            plan = {
                "component_name": component_name,
                "ports"         : ibd.get("ports", []),
                "links"         : ibd.get("links", []),
            }
            res = create_ibd(plan, r)
            if res.get("errors"):
                errors.extend(res["errors"])

        # Operation ADs — only when LLM relay is running
        try:
            import urllib.request, urllib.error
            req = urllib.request.Request(
                "http://127.0.0.1:3000/v1/chat/completions",
                data=b'{}', headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=2)
            relay_available = True
        except:
            relay_available = False
            print("[ApplyDirect] LLM relay not available — skipping operation ADs", file=sys.stderr)

        if relay_available and new_ops:
            from llm_node import llm_call
            from create_operation_ad import create_operation_ad
            from design_graph_unified import OP_AD_PROMPT
            import json as _j

            for op in new_ops:
                try:
                    prompt = OP_AD_PROMPT.format(
                        op_name    = op.get("name",""),
                        arguments  = _j.dumps(op.get("arguments",[]), indent=2),
                        return_type= op.get("return_type","void"),
                        rationale  = op.get("rationale",""),
                        req_ids    = ", ".join(op.get("req_ids",[])),
                        updated_ad = state.get("updated_ad",""),
                    )
                    mermaid = "flowchart TD\n" + llm_call(prompt)
                    if "```" in mermaid:
                        mermaid = "\n".join(l for l in mermaid.split("\n")
                                            if not l.strip().startswith("```"))
                    result = create_operation_ad(component_name, op["name"], mermaid, r)
                    op_ads.append({"operation": op["name"], "result": result})
                    print(f"[ApplyDirect] Op AD: {op['name']} → {result.get('success')}", file=sys.stderr)
                except Exception as e:
                    print(f"[ApplyDirect] Op AD error {op.get('name')}: {e}", file=sys.stderr)
                    op_ads.append({"operation": op.get("name",""), "error": str(e)})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ApplyDirect] ERROR: {e}", file=sys.stderr)
        print(tb, file=sys.stderr)
        errors.append(f"apply_design_state: {e}")

    return {
        "approved"       : True,
        "errors"         : errors,
        "new_operations" : state.get("new_operations", []),
        "operation_ads"  : op_ads,
    }
