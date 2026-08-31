"""
design_graph_unified.py
-----------------------
LangGraph pipeline for automated SysML design in Rhapsody.
Single shared llm_node — all LLM calls go through one place.

State flow per phase:
  <phase>_prompt_node  → sets llm_prompt + current_phase
  llm_node             → calls LLM, sets llm_response
  <phase>_parse_node   → parses llm_response, updates state

Full pipeline:
  ingest → classify_prompt → llm → classify_parse → group
         → pick_usecase → read_context
         → update_ad_prompt → llm → update_ad_parse
         → design_elements_prompt → llm → design_elements_parse
         → design_ibd_prompt → llm → design_ibd_parse
         → human_review → apply
         → fan_out_ad → [op_ad_prompt → llm → op_ad_parse → apply_op_ad]
         → next_usecase → (loop or END)
"""

import os
import sys
import json
import operator
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import Send, interrupt, Command

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from llm_node import llm_call, llm_json, extract_json
from config import RUNTIME_DIR


# ── State ─────────────────────────────────────────────────────────────────────

class DesignState(TypedDict):
    # Input
    component_name     : str
    requirement_source : str       # 'rhapsody' or 'excel'
    excel_path         : str       # path to Excel file (when source=excel)
    requirements       : list      # [{id, text, type, state, path}]
    use_cases          : list      # [{name, description, actors, requirements}]

    # Phase 1 output
    req_groups       : dict        # {usecase → [req dicts]}
    unmapped_reqs    : list

    # Queue
    pending_usecases : list
    current_usecase  : str
    current_reqs     : list

    # Context reads (per use case)
    existing_ad      : str         # analysis AD Mermaid
    existing_req_map : dict        # {action_id → [req_ids]} from existing AD
    bdd_dd           : str         # DD BDD Mermaid
    bdd_interfaces   : str         # Interfaces BDD Mermaid
    bdd_dd_name      : str         # actual DD diagram name in Rhapsody
    bdd_intf_name    : str         # actual interfaces diagram name in Rhapsody
    ibd              : str         # IBD Mermaid
    unrealized_intfs : list

    # Shared LLM bus
    current_phase    : str         # which phase is using llm_node
    llm_prompt       : str         # assembled prompt
    llm_response     : str         # raw LLM output

    # Phase outputs
    updated_ad       : str         # Phase 2a
    new_operations   : Annotated[list, operator.add]   # Phase 2b
    new_interfaces   : Annotated[list, operator.add]   # Phase 2b
    action_op_map    : dict        # Phase 2b
    bdd_delta        : dict        # Phase 2b
    ibd_delta        : dict        # Phase 3
    operation_ads    : Annotated[list, operator.add]   # Phase 4

    # Control
    approved         : bool
    human_feedback   : str
    errors           : Annotated[list, operator.add]


# Sub-state for fan-out
class OpADState(TypedDict):
    component_name : str
    operation      : dict
    updated_ad     : str
    llm_prompt     : str
    llm_response   : str


# ── Prompt templates ──────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are a requirements engineer. Map each requirement to the most relevant existing use case.

# Existing Use Cases
{use_cases}

# Requirements to Map
{requirements}

Rules:
- Only map to EXISTING use cases listed above — do NOT invent new ones
- If a requirement does not fit any use case, mark it as "unmapped"

Respond ONLY with valid JSON:
{{
  "mappings": [
    {{"req_id": "SRS_XXX_001", "usecase": "ExistingUseCaseName", "rationale": "brief reason"}}
  ],
  "unmapped": ["SRS_XXX_999"]
}}
"""

UPDATE_AD_PROMPT = """\
You are a SysML architect updating an AB12 Analysis Activity Diagram (AB12-Analysis perspective in Rhapsody).
This is the ANALYSIS level — it models WHAT the component must do (the problem), not HOW (the solution).

# Use Case: {usecase}

# Requirements to Cover
{requirements}

# Existing Analysis Activity Diagram (Mermaid)
{existing_ad}

# Requirements Already Covered by Existing Actions
{req_links}

Rules:
- If ALL requirements are already covered → reply exactly: NO_CHANGE
- Only ADD new abstract actions/decisions for uncovered requirements
- Keep all existing actions unchanged
- Every new action MUST have stereotype <<analysis>>
- Actions describe business-level steps — no implementation vocabulary, no C-code concepts
- MAX ~7 elements per diagram level; group more than that into a named subactivity
- Forbidden in analysis perspective: Call Operations, Activity Parameters, Swimlanes
- Fork/Join is allowed ONLY when the ordering of actions is genuinely unspecified
- After the flowchart, list which requirements each NEW action covers:

Requirements linked per action:
    <action_id>: <REQ_ID1>, <REQ_ID2>

Return ONLY the updated Mermaid flowchart followed by the requirements mapping.
"""

DESIGN_ELEMENTS_PROMPT = """\
You are a SysML architect working on AB12 Static Detailed Design.
Identify what new operations and interfaces are needed to implement the updated activity diagram.

# Use Case: {usecase}

# Requirements
{requirements}

# Updated Analysis AD
{updated_ad}

# Existing Operations + Attributes (BDD)
{bdd_dd}

# Existing Interfaces + Unrealized Gaps (BDD Interfaces)
{bdd_interfaces}

Rules:
- Reuse existing operations where possible — only add new ones if truly needed
- New operation names: follow existing naming convention from BDD (rb_<componentPrefix>_<VerbNoun>)
- Module names MUST follow: rb_<moduleShortName>_<ModuleName>  (e.g. rb_hvsm_HighVoltageSupplyMonitoring)
- Interface names MUST follow: rb_<moduleShortName>_<InterfaceName>Intf  (e.g. rb_hvsm_ProvideConfigStructureIntf)
- Use existing types from BDD — do NOT invent new types
- For each action in the AD, map it to an existing or new operation
- New functionality: map each new action to exactly ONE module (1 action → 1 module)

Respond ONLY with valid JSON:
{{
  "action_op_map": {{"AD_action_name": "operation_name"}},
  "new_operations": [
    {{
      "name": "rb_<prefix>_NewOp",
      "return_type": "<existing_result_type>",
      "arguments": [{{"name": "param_name", "type": "<existing_type>"}}],
      "visibility": "public",
      "stereotypes": [],
      "rationale": "needed for SRS_XXX",
      "req_ids": ["SRS_XXX_001"]
    }}
  ],
  "new_interfaces": [
    {{
      "name": "rb_<moduleShortName>_<InterfaceName>Intf",
      "stereotypes": ["Interface"],
      "operations": [],
      "attributes": [],
      "realized_by": "{usecase}"
    }}
  ],
  "realizations": [
    {{"specific": "{usecase}", "general": "rb_<moduleShortName>_<InterfaceName>Intf"}}
  ]
}}
"""

DESIGN_IBD_PROMPT = """\
You are a SysML architect working on AB12 Static Detailed Design (IBD level).
Propose IBD changes (new ports) for new interfaces.

# New Interfaces
{new_interfaces}

# Current IBD
{ibd}

Rules:
- Only add ports for genuinely new interfaces not already in the IBD
- Port names MUST follow: rb_<moduleShortName>_<PortName>Prt  (e.g. rb_hvsm_VoltageRegulatorMonitoringPrt)
- Provided interfaces: +, Required: -
- Only propose what's missing
- Two IBDs are required per module: one at the DetailedDesign package level (showing the module within
  its SW component and its connections to sibling modules) and one inside the module package (showing
  only the module's own ports). Indicate which IBD each proposed port belongs to.

Respond ONLY with valid JSON:
{{
  "ports": [
    {{
      "name": "rb_<moduleShortName>_<PortName>Prt",
      "provided": ["rb_<moduleShortName>_<InterfaceName>Intf"],
      "required": [],
      "ibd_level": "dd_package"
    }}
  ],
  "links": []
}}
"""

OP_AD_PROMPT = """\
You are a SysML architect generating an AB12 Operation Activity Diagram (AB12-Design perspective in Rhapsody).
This is the DESIGN level — one activity diagram per operation, showing HOW the operation behaves.
It is distinct from the Analysis Activity Diagram; the analysis AD is provided only as context.

# Operation
Name        : {op_name}
Arguments   : {arguments}
Return type : {return_type}
Rationale   : {rationale}
Requirements: {req_ids}

# Analysis Activity Diagram (context only — shows the analysis actions this operation implements)
{updated_ad}

Rules:
- Use ([Start]) and ([End]) for start/end nodes
- Use {{condition?}} for decision nodes
- Use ((merge)) for merge nodes after branches
- Every action MUST have stereotype <<design>>
- Use between 1 and 3 design actions — enough to show the main idea of the implementation
  (more than 5 requires strong justification)
- Design actions are ABSTRACT — show design intent, not C-code detail
- Do NOT use fork/join nodes (ordering must be fully defined at design level)
- Do NOT use Call Operations or Call Behaviours as the primary mechanism
- Return ONLY the Mermaid flowchart

flowchart TD
"""


# ── Node 1: Ingest ────────────────────────────────────────────────────────────

def ingest_node(state: DesignState) -> dict:
    """
    Read approved requirements + use cases.
    Source: 'rhapsody' (default) or 'excel'.
    """
    component_name = state["component_name"]
    source         = state.get("requirement_source", "rhapsody")
    print(f"[Ingest] Component: {component_name}, Source: {source}")

    # Initialize debug logger
    from debug_logger import DebugLogger
    log = DebugLogger(component_name)
    log.step("ingest", f"Starting ingestion — source: {source}")

    try:
        import win32com.client
        from read_usecases import find_package_recursive, collect_usecases
        from read_all_requirements import (get_rhapsody, find_srs_package,
                                           collect_requirements_recursive)

        _, project = get_rhapsody()

        # ── Use Cases (always from Rhapsody) ──────────────────────────────────
        comp_pkg  = find_package_recursive(project, component_name)
        use_cases = []
        if comp_pkg:
            raw_ucs   = collect_usecases(comp_pkg)
            use_cases = [
                {
                    "name"        : uc["name"],
                    "description" : uc.get("description", ""),
                    "requirements": uc.get("linked_requirements", []),
                    "diagrams"    : uc.get("linked_diagrams", []),
                }
                for uc in raw_ucs
            ]
            log.step("ingest", f"Use cases found: {len(use_cases)}")
            log.data("use_cases", [{"name": u["name"],
                                    "description": u.get("description","")[:200],
                                    "linked_reqs": len(u["requirements"])}
                                    for u in use_cases])
            print(f"[Ingest] Use cases: {len(use_cases)}")

        # ── Requirements ──────────────────────────────────────────────────────
        requirements = []

        if source == "excel":
            excel_path = state.get("excel_path", "")
            if not excel_path:
                import glob
                files = glob.glob(f"RUNTIME_DIR + "/"{component_name}*.xlsx") + \
                        glob.glob("RUNTIME_DIR + "/"requirements*.xlsx")
                excel_path = files[0] if files else ""

            if not excel_path:
                print("[Ingest] ERROR: No Excel file path provided")
                return {"errors": ["ingest_node: No Excel file path provided"]}

            print(f"[Ingest] Reading Excel: {excel_path}")
            log.step("ingest", f"Reading Excel: {excel_path}")

            try:
                from parse_requirements_xlsx import parse_requirements
                result = parse_requirements(
                    excel_path,
                    state_filter=["approved"],   # only approved
                )
                requirements = []
                for r in result.get("requirements", []):
                    requirements.append({
                        "id"   : r.get("id", ""),
                        "text" : r.get("requirement", ""),
                        "type" : r.get("type", ""),
                        "state": r.get("state", ""),
                        "path" : r.get("id", ""),
                        "realized_by_module": r.get("realized_by_module", ""),
                    })
                print(f"[Ingest] Excel requirements (approved): {len(requirements)}")
                log.step("ingest", f"Excel requirements loaded: {len(requirements)}")
            except Exception as ex:
                print(f"[Ingest] Excel read failed: {ex}")
                return {"errors": [f"ingest_node: Excel read failed: {ex}"]}

        else:
            # Default: read from Rhapsody SRS package
            srs_pkg, srs_name = find_srs_package(project, component_name)
            if srs_pkg:
                collect_requirements_recursive(srs_pkg, requirements)
                print(f"[Ingest] Rhapsody requirements: {len(requirements)} from {srs_name}")
            else:
                print("[Ingest] WARNING: SRS package not found")

        all_requirements = list(requirements)  # save before filter
        # Filter to approved requirements only
        requirements = [r for r in all_requirements
                        if r.get("state", "").lower() == "approved"]
        print(f"[Ingest] Approved requirements: {len(requirements)}")

        if not requirements:
            log.step("ingest", "⚠️ No approved requirements found")
            print("[Ingest] WARNING: No approved requirements found")
            all_states = list(set(r.get("state","") for r in all_requirements))
            import json as _j
            with open(os.path.join(RUNTIME_DIR,
                                   f"_progress_{component_name}.json"),
                      "w", encoding="utf-8") as pf:
                _j.dump({
                    "step"    : "no_approved_requirements",
                    "warning" : f"No approved requirements found for {component_name}. "
                                f"Found {len(all_requirements)} requirements "
                                f"with states: {all_states}. "
                                f"Please set requirements to 'approved' state in Rhapsody.",
                    "total_reqs" : 0,
                    "use_cases"  : [u["name"] for u in use_cases],
                }, pf, indent=2)
            return {
                "requirements"     : [],
                "use_cases"        : use_cases,
                "req_groups"       : {},
                "unmapped_reqs"    : [],
                "pending_usecases" : [],
                "new_operations"   : [],
                "new_interfaces"   : [],
                "operation_ads"    : [],
                "errors"           : [f"No approved requirements found. States found: {all_states}"],
            }
        log.step("ingest", f"Approved requirements: {len(requirements)}")
        log.data("requirements", [{"id": r["id"], "text": r.get("text","")[:100],
                                   "state": r.get("state","")} 
                                  for r in requirements])
        log.summary("ingest", {
            "total_requirements": len(requirements),
            "total_use_cases"   : len(use_cases),
            "use_case_names"    : [u["name"] for u in use_cases],
        })

        progress = {
            "step"        : "ingest_complete",
            "requirements": [{"id": r["id"], "text": r.get("text","")[:120]} 
                            for r in requirements],
            "use_cases"   : [u["name"] for u in use_cases],
            "total_reqs"  : len(requirements),
            "total_ucs"   : len(use_cases),
        }
        progress_file = os.path.join(
            RUNTIME_DIR,
            f"_progress_{component_name}.json"
        )
        with open(progress_file, "w", encoding="utf-8") as pf:
            import json as _j
            _j.dump(progress, pf, indent=2)

        return {
            "requirements"     : requirements,
            "use_cases"        : use_cases,
            "req_groups"       : {},
            "unmapped_reqs"    : [],
            "pending_usecases" : [],
            "new_operations"   : [],
            "new_interfaces"   : [],
            "operation_ads"    : [],
            "errors"           : [],
        }

    except Exception as e:
        print(f"[Ingest] ERROR: {e}")
        return {"errors": [f"ingest_node: {e}"]}


# ── Phase 1: Classify ─────────────────────────────────────────────────────────

def classify_prompt_node(state: DesignState) -> dict:
    """
    LLM maps SW requirements (SRS) to existing use cases.
    Use case context (actors + linked system reqs) helps the LLM understand
    what each use case covers.
    """
    uc_text = "\n".join(
        f"- **{u['name']}**: {u.get('description','')[:20000]}"
        for u in state["use_cases"]
    )
    req_text = "\n".join(
        f"- {r['id']}: {r.get('text', '')[:1000]}"
        for r in state["requirements"]
    )
    print(f"[Classify] Mapping {len(state['requirements'])} SW reqs "
          f"to {len(state['use_cases'])} use cases via LLM")
    prompt = CLASSIFY_PROMPT.format(use_cases=uc_text, requirements=req_text)

    # Debug log
    try:
        from debug_logger import DebugLogger
        log = DebugLogger(state.get("component_name","unknown"))
        log.separator("classify_prompt")
        log.llm_prompt("classify", prompt)
    except: pass

    return {
        "current_phase": "classify",
        "llm_prompt"  : prompt,
    }


def classify_parse_node(state: DesignState) -> dict:
    if state.get("current_phase") == "classify_skip":
        return {"req_groups": {"_mappings": []}, "unmapped_reqs": []}
    try:
        from debug_logger import DebugLogger
        log = DebugLogger(state.get("component_name","unknown"))
        log.llm_response("classify", state["llm_response"])
    except: pass
    result   = extract_json(state["llm_response"], default={})
    mappings = result.get("mappings", [])
    unmapped = result.get("unmapped", [])
    print(f"[Classify] LLM mapped: {len(mappings)}, Unmapped: {len(unmapped)}")
    return {
        "req_groups"  : {"_mappings": mappings},
        "unmapped_reqs": unmapped,
    }


# ── Group ─────────────────────────────────────────────────────────────────────

def group_node(state: DesignState) -> dict:
    """Group SW requirements by use case based on LLM mappings."""
    req_by_id = {r["id"]: r for r in state["requirements"]}
    groups: dict = {}

    # Use LLM mappings (SW req → use case)
    llm_mappings = state.get("req_groups", {}).get("_mappings", [])
    for m in llm_mappings:
        uc, rid = m["usecase"], m["req_id"]
        if rid in req_by_id:
            groups.setdefault(uc, [])
            if req_by_id[rid] not in groups[uc]:
                groups[uc].append(req_by_id[rid])

    print(f"[Group] Use cases: {list(groups.keys())}")
    print(f"[Group] Total reqs grouped: "
          f"{sum(len(v) for v in groups.values())}")
    return {
        "req_groups"      : groups,
        "pending_usecases": list(groups.keys()),
    }


# ── Pick Use Case ─────────────────────────────────────────────────────────────

def pick_usecase_node(state: DesignState) -> dict:
    pending = list(state.get("pending_usecases", []))
    if not pending:
        return {"current_usecase": "", "current_reqs": []}
    current = pending.pop(0)
    reqs    = state["req_groups"].get(current, [])
    print(f"\n[Pick] Use case: {current} ({len(reqs)} reqs)")
    return {
        "current_usecase"  : current,
        "current_reqs"     : reqs,
        "pending_usecases" : pending,
        "updated_ad"       : "",
        "ibd_delta"        : {},
        "bdd_delta"        : {},
        "action_op_map"    : {},
        "llm_prompt"       : "",
        "llm_response"     : "",
    }


# ── Read Context ──────────────────────────────────────────────────────────────

def read_context_node(state: DesignState) -> dict:
    print(f"[Context] Reading diagrams for: {state['current_usecase']}")
    try:
        import win32com.client
        from rhapsody_com import get_sw_model
        from read_detailed_ad import (find_package_recursive,
                                      find_dd_packages,
                                      find_behavioral_diagram,
                                      read_detailed_ad)
        from ad_to_mermaid import mermaid_with_context as ad_mermaid
        from read_bdd import read_bdd_from_package, find_unrealized_interfaces
        from bdd_to_mermaid import mermaid_with_context as bdd_mermaid
        from read_ibd import read_ibd_from_package
        from ibd_to_mermaid import mermaid_with_context as ibd_mermaid

        r  = win32com.client.GetActiveObject("Rhapsody2.Application")
        p  = r.activeProject()
        sw = get_sw_model(p) or p

        comp_pkg = find_package_recursive(sw, state["component_name"])
        dd_pkgs  = find_dd_packages(comp_pkg)
        dd_pkg   = next((x for x in dd_pkgs if "Cfg" not in x.name), dd_pkgs[0])

        # Analysis AD
        uc = state["current_usecase"]
        ad_str  = f"%% No existing AD for: {uc}"
        diagram, _ = find_behavioral_diagram(dd_pkgs, uc + "AD")
        if not diagram:
            bd = dd_pkg.behavioralDiagrams
            uc_norm = uc.lower().replace(" ", "").replace("_", "")
            for i in range(1, bd.Count + 1):
                d = bd.Item(i)
                d_norm = d.name.lower().replace(" ", "").replace("_", "")
                if uc_norm in d_norm or d_norm.startswith(uc_norm):
                    diagram = d; break
        if diagram:
            ad_str = ad_mermaid(read_detailed_ad(diagram))
            print(f"[Context] AD: {diagram.name}")

        # Extract existing action→req mapping from the AD metadata
        import re as _re
        existing_req_map = {}
        if "Requirements linked per action:" in ad_str:
            section = ad_str.split("Requirements linked per action:")[1]
            for match in _re.finditer(r'(\w+):\s*((?:SRS_\w+(?:,\s*)?)+)', section):
                action_id = match.group(1).strip()
                reqs = [r.strip() for r in match.group(2).split(",") if r.strip()]
                existing_req_map[action_id] = reqs
        print(f"[Context] Existing req mappings: {len(existing_req_map)} actions")

        # Find BDD diagram names dynamically
        bdd_dd_name    = None
        bdd_intf_name  = None
        try:
            for i in range(1, dd_pkg.objectModelDiagrams.Count + 1):
                d = dd_pkg.objectModelDiagrams.Item(i)
                if "Interface" in d.name or "Intf" in d.name:
                    bdd_intf_name = d.name
                else:
                    bdd_dd_name = d.name
        except: pass
        print(f"[Context] BDD diagrams: DD={bdd_dd_name}, Intf={bdd_intf_name}")

        # BDDs
        bdds_dd   = read_bdd_from_package(dd_pkg, bdd_dd_name)   if bdd_dd_name   else []
        bdds_intf = read_bdd_from_package(dd_pkg, bdd_intf_name) if bdd_intf_name else []
        unrealized = find_unrealized_interfaces(dd_pkg, comp_pkg)

        bdd_dd_str   = bdd_mermaid(bdds_dd[0])   if bdds_dd   else "%% No DD BDD"
        bdd_intf_str = bdd_mermaid(bdds_intf[0],
                           unrealized=unrealized) if bdds_intf else "%% No Interfaces BDD"

        # IBD
        comp_cls = comp_pkg.classes.Item(1)
        ibd_data = read_ibd_from_package(dd_pkg, comp_cls=comp_cls)
        ibd_str  = ibd_mermaid(ibd_data)

        return {
            "existing_ad"    : ad_str,
            "existing_req_map": existing_req_map,
            "bdd_dd"         : bdd_dd_str,
            "bdd_interfaces" : bdd_intf_str,
            "bdd_dd_name"    : bdd_dd_name or "DetailedDesignBDD",
            "bdd_intf_name"  : bdd_intf_name or "DetailedDesignInterfacesBDD",
            "ibd"            : ibd_str,
            "unrealized_intfs": unrealized.get("unrealized", []),
        }
    except Exception as e:
        print(f"[Context] ERROR: {e}")
        return {"errors": [f"read_context_node: {e}"]}


# ── Phase 2a: Update AD ───────────────────────────────────────────────────────

def update_ad_prompt_node(state: DesignState) -> dict:
    req_text = "\n".join(f"- {r['id']}: {r.get('text', r.get('shall',''))[:1000]}"
                         for r in state["current_reqs"])

    existing_ad = state["existing_ad"]

    # Strip header and metadata — only send the flowchart to LLM
    # Find where the actual flowchart starts
    flowchart_only = existing_ad
    for keyword in ["flowchart TD", "flowchart LR", "graph TD", "graph LR"]:
        if keyword in existing_ad:
            flowchart_only = existing_ad[existing_ad.index(keyword):]
            break

    # Strip everything after "Requirements linked per action:" 
    for marker in ["Requirements linked per action:", "Rules:"]:
        if marker in flowchart_only:
            flowchart_only = flowchart_only[:flowchart_only.index(marker)].strip()
            break

    # Extract requirement links from metadata to show LLM what's covered
    import re as _re
    links = _re.findall(r'(\w+):\s*(SRS_\w+(?:,\s*SRS_\w+)*)', existing_ad)
    if links:
        req_links = "\n".join(f"- Action '{a}' covers: {r.strip()}" for a, r in links)
    else:
        req_links = "(No requirement links found in existing AD)"

    return {
        "current_phase": "update_ad",
        "llm_prompt"  : UPDATE_AD_PROMPT.format(
            usecase     = state["current_usecase"],
            requirements= req_text,
            existing_ad = flowchart_only,
            req_links   = req_links,
        ),
    }


def update_ad_parse_node(state: DesignState) -> dict:
    updated = state["llm_response"].strip()

    # Handle NO_CHANGE response
    if "NO_CHANGE" in updated[:30]:
        print("[UpdateAD] LLM says NO_CHANGE — keeping existing AD")
        # Return only the clean flowchart part
        existing = state.get("existing_ad", "")
        for keyword in ["flowchart TD", "flowchart LR", "graph TD", "graph LR"]:
            if keyword in existing:
                existing = existing[existing.index(keyword):]
                break
        for marker in ["Requirements linked per action:", "Rules:"]:
            if marker in existing:
                existing = existing[:existing.index(marker)].strip()
                break

        # Extract existing req links from the AD metadata
        import re as _re
        req_mapping = {}
        if "Requirements linked per action:" in state.get("existing_ad", ""):
            section = state["existing_ad"].split("Requirements linked per action:")[1]
            for match in _re.finditer(r'(\w+):\s*((?:SRS_\w+(?:,\s*)?)+)', section):
                action_id = match.group(1).strip()
                reqs = [r.strip() for r in match.group(2).split(",")]
                req_mapping[action_id] = reqs
            print(f"[UpdateAD] Existing req mappings carried forward: {req_mapping}")

        # Re-attach requirements section so create_or_update_ad can use it
        if req_mapping:
            req_lines = "\n".join(
                f"    {aid}: {', '.join(rids)}"
                for aid, rids in req_mapping.items()
            )
            existing = existing + "\n\nRequirements linked per action:\n" + req_lines

        return {
            "updated_ad"      : existing,
            "existing_req_map": req_mapping,
        }

    # Strip markdown fences
    if "```" in updated:
        lines = updated.split("\n")
        updated = "\n".join(l for l in lines
                            if not l.strip().startswith("```"))

    # Separate flowchart from requirement mapping
    flowchart = updated
    new_req_mapping = {}
    if "Requirements linked per action:" in updated:
        parts = updated.split("Requirements linked per action:")
        flowchart = parts[0].strip()
        import re as _re
        for match in _re.finditer(r'(\w+):\s*((?:SRS_\w+(?:,\s*)?)+)', parts[1]):
            action_id = match.group(1).strip()
            reqs = [r.strip() for r in match.group(2).split(",") if r.strip()]
            new_req_mapping[action_id] = reqs
        print(f"[UpdateAD] New req mappings: {new_req_mapping}")

    # Merge with existing req map
    merged = dict(state.get("existing_req_map", {}))
    merged.update(new_req_mapping)

    # Normalize flowchart keyword
    flowchart = flowchart.replace("flowchart TD", "graph TD")\
                         .replace("flowchart LR", "graph LR")

    # Re-attach requirements section to updated_ad so it travels with the
    # mermaid string all the way to create_or_update_ad → from_mermaid.
    if merged:
        req_lines = "\n".join(
            f"    {aid}: {', '.join(rids)}"
            for aid, rids in merged.items()
        )
        full_ad = flowchart + "\n\nRequirements linked per action:\n" + req_lines
    else:
        full_ad = flowchart

    print(f"[UpdateAD] Updated AD: {len(full_ad)} chars, total req mappings: {len(merged)}")
    return {
        "updated_ad"      : full_ad,
        "existing_req_map": merged,
    }


# ── Phase 2b: Design Elements ─────────────────────────────────────────────────

def design_elements_prompt_node(state: DesignState) -> dict:
    req_text = "\n".join(f"- {r['id']}: {r.get('text', r.get('shall',''))}"
                         for r in state["current_reqs"])
    return {
        "current_phase": "design_elements",
        "llm_prompt"  : DESIGN_ELEMENTS_PROMPT.format(
            usecase        = state["current_usecase"],
            requirements   = req_text,
            updated_ad     = state["updated_ad"],
            bdd_dd         = state["bdd_dd"],
            bdd_interfaces = state["bdd_interfaces"],
        ),
    }


def design_elements_parse_node(state: DesignState) -> dict:
    result    = extract_json(state["llm_response"], default={})
    new_ops   = result.get("new_operations", [])
    new_intfs = result.get("new_interfaces", [])
    for op in new_ops:
        op["usecase"] = state["current_usecase"]
    print(f"[DesignElements] Ops: {len(new_ops)}, Intfs: {len(new_intfs)}")
    return {
        "new_operations": new_ops,
        "new_interfaces": new_intfs,
        "action_op_map" : result.get("action_op_map", {}),
        "bdd_delta"     : {
            "classes"        : new_intfs,
            "realizations"   : result.get("realizations", []),
            "generalizations": [],
        },
    }


# ── Phase 3: Design IBD ───────────────────────────────────────────────────────

def design_ibd_prompt_node(state: DesignState) -> dict:
    if not state.get("new_interfaces"):
        # Skip IBD if no new interfaces
        return {
            "current_phase": "design_ibd_skip",
            "llm_prompt"  : "",
            "ibd_delta"   : {"ports": [], "links": []},
        }
    return {
        "current_phase": "design_ibd",
        "llm_prompt"  : DESIGN_IBD_PROMPT.format(
            new_interfaces = json.dumps(
                state.get("new_interfaces", []), indent=2),
            ibd            = state["ibd"],
        ),
    }


def design_ibd_parse_node(state: DesignState) -> dict:
    if state.get("current_phase") == "design_ibd_skip":
        return {}
    result = extract_json(state["llm_response"],
                          default={"ports": [], "links": []})
    print(f"[DesignIBD] Ports: {len(result.get('ports',[]))}")
    return {"ibd_delta": result}


# ── Human Review ──────────────────────────────────────────────────────────────

def human_review_node(state: DesignState) -> dict:
    print(f"\n[HumanReview] Use case: {state['current_usecase']}")
    print(f"  New ops  : {len(state.get('new_operations',[]))}")
    print(f"  New intfs: {len(state.get('new_interfaces',[]))}")
    print(f"  IBD ports: {len(state.get('ibd_delta',{}).get('ports',[]))}")

    feedback = interrupt({
        "component"     : state["component_name"],
        "usecase"       : state["current_usecase"],
        "updated_ad"    : state.get("updated_ad", ""),
        "new_operations": state.get("new_operations", []),
        "new_interfaces": state.get("new_interfaces", []),
        "ibd_delta"     : state.get("ibd_delta", {}),
        "message"       : "Review the proposed design changes. Reply 'apply' to approve or provide feedback.",
    })

    # Fix 3: Handle both string resume ("apply") and dict feedback
    if isinstance(feedback, str):
        approved = feedback.strip().lower() in ("apply", "approve", "yes", "ok")
        fb_text  = "" if approved else feedback.strip()
    elif isinstance(feedback, dict):
        approved = feedback.get("approved", False)
        fb_text  = feedback.get("feedback", "")
    else:
        approved = False
        fb_text  = str(feedback)

    return {
        "approved"      : approved,
        "human_feedback": fb_text,
    }


# ── Apply to Rhapsody ─────────────────────────────────────────────────────────

def apply_node(state: DesignState) -> dict:
    if not state.get("approved"):
        print("[Apply] Not approved — skipping")
        return {}
    print(f"[Apply] Applying changes for: {state['current_usecase']}")
    errors = []
    try:
        import win32com.client
        from create_bdd import create_bdd_from_plan
        from create_ibd import create_ibd

        r = win32com.client.GetActiveObject("Rhapsody2.Application")
        print(f"[Apply] Connected to Rhapsody: {r.activeProject().name}", file=sys.stderr)

        # ── New operations → add to existing module class ─────────────────────
        new_ops = state.get("new_operations", [])
        print(f"[Apply] New ops: {len(new_ops)}", file=sys.stderr)
        if new_ops:
            from create_bdd import create_bdd_from_plan
            ops_plan = {
                "component_name" : state["component_name"],
                "classes"        : [{
                    "name"       : state["component_name"],
                    "stereotypes": [],
                    "operations" : new_ops,
                    "attributes" : [],
                }],
                "realizations"   : [],
                "generalizations": [],
            }
            res = create_bdd_from_plan(ops_plan, r,
                                       diagram_name=state.get("bdd_dd_name","DetailedDesignBDD"))
            print(f"[Apply] create_bdd_from_plan result: {res}", file=sys.stderr)
            if res.get("errors"):
                errors.extend(res["errors"])
        bdd = state.get("bdd_delta", {})
        if bdd.get("classes") or bdd.get("realizations"):
            plan = {
                "component_name" : state["component_name"],
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
                "component_name": state["component_name"],
                "ports"         : ibd.get("ports", []),
                "links"         : ibd.get("links", []),
            }
            res = create_ibd(plan, r)
            if res.get("errors"):
                errors.extend(res["errors"])

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Apply] EXCEPTION: {e}", file=sys.stderr)
        print(tb, file=sys.stderr)
        errors.append(f"apply_node: {e}\n{tb}")

    # ── Operation ADs ────────────────────────────────────────────────────────
    op_ads = []
    new_ops = state.get("new_operations", [])
    if new_ops:
        try:
            from create_operation_ad import create_operation_ad
            from llm_node import llm_call
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
                    print(f"[Apply] Generating AD for: {op.get('name')}", file=sys.stderr)
                    mermaid = "flowchart TD\n" + llm_call(prompt)
                    if "```" in mermaid:
                        mermaid = "\n".join(l for l in mermaid.split("\n")
                                            if not l.strip().startswith("```"))
                    result = create_operation_ad(state["component_name"], op["name"], mermaid, r)
                    op_ads.append({"operation": op["name"], "result": result})
                    print(f"[Apply] Op AD: {op['name']} → {result.get('success')}", file=sys.stderr)
                except Exception as e:
                    print(f"[Apply] Op AD error {op.get('name')}: {e}", file=sys.stderr)
                    op_ads.append({"operation": op.get("name",""), "error": str(e)})
        except Exception as e:
            print(f"[Apply] Op AD generation failed: {e}", file=sys.stderr)
            errors.append(f"op_ad: {e}")

    print(f"[Apply] Done. Errors: {len(errors)}", file=sys.stderr)
    return {"applied": True, "errors": errors, "operation_ads": op_ads}


# ── Fan-out: Operation ADs ────────────────────────────────────────────────────

def fan_out_ad_node(state: DesignState):
    """Fan out operation AD generation — one per new operation."""
    from langgraph.types import Command as LGCommand
    ops = [op for op in state.get("new_operations", [])
           if op.get("usecase") == state["current_usecase"]]

    if not ops or not state.get("approved"):
        print("[FanOut] No ops to generate ADs for")
        return {}

    print(f"[FanOut] {len(ops)} operation ADs to generate")
    # Generate ADs sequentially (no true fan-out in this version)
    for op in ops:
        try:
            import win32com.client
            from create_operation_ad import create_operation_ad
            from mermaid_to_ad import from_mermaid
            from llm_node import llm_call

            op_name = op.get("name", "")
            prompt = OP_AD_PROMPT.format(
                op_name    = op_name,
                arguments  = __import__("json").dumps(op.get("arguments", []), indent=2),
                return_type= op.get("return_type", "void"),
                rationale  = op.get("rationale", ""),
                req_ids    = ", ".join(op.get("req_ids", [])),
                updated_ad = state.get("updated_ad", ""),
            )
            print(f"[FanOut] Generating AD for: {op_name}")
            mermaid = "flowchart TD\n" + llm_call(prompt)
            if "```" in mermaid:
                mermaid = "\n".join(l for l in mermaid.split("\n")
                                    if not l.strip().startswith("```"))

            r = win32com.client.GetActiveObject("Rhapsody2.Application")
            result = create_operation_ad(state["component_name"], op_name, mermaid, r)
            print(f"[FanOut] {op_name}: {result.get('success')}")
        except Exception as e:
            print(f"[FanOut] ERROR {op.get('name')}: {e}")

    return {}


# ── Phase 4: Operation AD ─────────────────────────────────────────────────────

def op_ad_prompt_node(state: OpADState) -> dict:
    op = state["operation"]
    return {
        "current_phase": "op_ad",
        "llm_prompt"  : OP_AD_PROMPT.format(
            op_name    = op.get("name", ""),
            arguments  = json.dumps(op.get("arguments", []), indent=2),
            return_type= op.get("return_type", "void"),
            rationale  = op.get("rationale", ""),
            req_ids    = ", ".join(op.get("req_ids", [])),
            updated_ad = state.get("updated_ad", ""),
        ),
    }


def op_ad_parse_node(state: OpADState) -> dict:
    mermaid = "flowchart TD\n" + state["llm_response"].strip()
    if "```" in mermaid:
        lines   = mermaid.split("\n")
        mermaid = "\n".join(l for l in lines if not l.startswith("```"))
    return {"llm_response": mermaid}   # reuse field as parsed output


def apply_op_ad_node(state: OpADState) -> dict:
    op      = state["operation"]
    mermaid = state["llm_response"]
    name    = op.get("name", "")
    print(f"[ApplyOpAD] Creating AD for: {name}")
    try:
        import win32com.client
        from create_operation_ad import create_operation_ad
        r      = win32com.client.GetActiveObject("Rhapsody2.Application")
        result = create_operation_ad(
            state["component_name"], name, mermaid, r)
        print(f"[ApplyOpAD] {name}: {result.get('success')}")
        return {"operation_ads": [{"operation": name, "result": result}]}
    except Exception as e:
        print(f"[ApplyOpAD] ERROR {name}: {e}")
        return {"operation_ads": [{"operation": name, "error": str(e)}]}


# ── Next Use Case ─────────────────────────────────────────────────────────────

def next_usecase_node(state: DesignState) -> dict:
    print(f"[Next] Pending: {len(state.get('pending_usecases',[]))}")
    return {}


# ── Shared LLM Node ───────────────────────────────────────────────────────────

def llm_node(state) -> dict:
    """Single shared LLM call node. Reads llm_prompt, writes llm_response."""
    prompt = state.get("llm_prompt", "")
    if not prompt:
        return {"llm_response": ""}
    phase = state.get("current_phase", "unknown")
    component = state.get("component_name", "unknown")
    print(f"[LLM] Phase: {phase} | Prompt length: {len(prompt)} chars")

    # Debug log — full prompt
    try:
        from debug_logger import DebugLogger
        log = DebugLogger(component)
        log.separator(f"LLM CALL: {phase}")
        log.llm_prompt(phase, prompt)
    except: pass

    try:
        response = llm_call(
            prompt,
            fallback_interrupt={
                "phase"  : phase,
                "message": f"LLM unavailable for phase '{phase}'. "
                           "Please provide the response manually.",
            }
        )
        resp_str = response if isinstance(response, str) else str(response)
        print(f"[LLM] Response length: {len(resp_str)} chars")

        # Debug log — full response
        try:
            log.llm_response(phase, resp_str)
        except: pass

        return {"llm_response": resp_str}
    except Exception as e:
        print(f"[LLM] ERROR: {e}")
        try: log.error(f"llm_node:{phase}", str(e))
        except: pass
        return {"llm_response": "", "errors": [f"llm_node ({phase}): {e}"]}


# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_ingest(state: DesignState) -> str:
    """Skip pipeline if no approved requirements found."""
    if not state.get("requirements"):
        return END
    return "classify_prompt_node"
    if state.get("approved"):
        return "apply_node"
    return "design_elements_prompt_node"   # re-run with feedback


def route_after_review(state: DesignState) -> str:
    if state.get("approved"):
        return "apply_node"
    return "design_elements_prompt_node"


def route_after_next(state: DesignState) -> str:
    if state.get("pending_usecases"):
        return "pick_usecase_node"
    return END


def route_ibd_skip(state: DesignState) -> str:
    if state.get("current_phase") == "design_ibd_skip":
        return "design_ibd_parse_node"   # parse immediately (returns empty)
    return "llm_node"                    # normal LLM call


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(DesignState)

    # ── Main pipeline nodes ───────────────────────────────────────────────────
    g.add_node("ingest_node",                ingest_node)

    # Phase 1
    g.add_node("classify_prompt_node",       classify_prompt_node)
    g.add_node("llm_node",                   llm_node)
    g.add_node("classify_parse_node",        classify_parse_node)
    g.add_node("group_node",                 group_node)

    # Queue
    g.add_node("pick_usecase_node",          pick_usecase_node)
    g.add_node("read_context_node",          read_context_node)

    # Phase 2a
    g.add_node("update_ad_prompt_node",      update_ad_prompt_node)
    g.add_node("update_ad_llm_node",         llm_node)
    g.add_node("update_ad_parse_node",       update_ad_parse_node)

    # Phase 2b
    g.add_node("design_elements_prompt_node",design_elements_prompt_node)
    g.add_node("design_elements_llm_node",   llm_node)
    g.add_node("design_elements_parse_node", design_elements_parse_node)

    # Phase 3
    g.add_node("design_ibd_prompt_node",     design_ibd_prompt_node)
    g.add_node("design_ibd_llm_node",        llm_node)
    g.add_node("design_ibd_parse_node",      design_ibd_parse_node)

    # Review + Apply
    g.add_node("human_review_node",          human_review_node)
    g.add_node("apply_node",                 apply_node)

    # Phase 4 fan-out
    g.add_node("fan_out_ad_node",            fan_out_ad_node)
    g.add_node("op_ad_prompt_node",          op_ad_prompt_node)
    g.add_node("op_ad_llm_node",             llm_node)
    g.add_node("op_ad_parse_node",           op_ad_parse_node)
    g.add_node("apply_op_ad_node",           apply_op_ad_node)

    g.add_node("next_usecase_node",          next_usecase_node)

    # ── Edges ─────────────────────────────────────────────────────────────────
    g.set_entry_point("ingest_node")

    # Phase 1 — skip if no approved requirements
    g.add_conditional_edges("ingest_node", route_after_ingest, {
        "classify_prompt_node": "classify_prompt_node",
        END                   : END,
    })
    g.add_edge("classify_prompt_node",       "llm_node")
    g.add_edge("llm_node",                   "classify_parse_node")
    g.add_edge("classify_parse_node",        "group_node")
    g.add_edge("group_node",                 "pick_usecase_node")

    # Per use case
    g.add_edge("pick_usecase_node",          "read_context_node")
    g.add_edge("read_context_node",          "update_ad_prompt_node")

    # Phase 2a
    g.add_edge("update_ad_prompt_node",      "update_ad_llm_node")
    g.add_edge("update_ad_llm_node",         "update_ad_parse_node")
    g.add_edge("update_ad_parse_node",       "design_elements_prompt_node")

    # Phase 2b
    g.add_edge("design_elements_prompt_node","design_elements_llm_node")
    g.add_edge("design_elements_llm_node",   "design_elements_parse_node")
    g.add_edge("design_elements_parse_node", "design_ibd_prompt_node")

    # Phase 3
    g.add_conditional_edges("design_ibd_prompt_node", route_ibd_skip, {
        "llm_node"             : "design_ibd_llm_node",
        "design_ibd_parse_node": "design_ibd_parse_node",
    })
    g.add_edge("design_ibd_llm_node",        "design_ibd_parse_node")
    g.add_edge("design_ibd_parse_node",      "human_review_node")

    # Review
    g.add_conditional_edges("human_review_node", route_after_review, {
        "apply_node"                 : "apply_node",
        "design_elements_prompt_node": "design_elements_prompt_node",
    })

    # Apply + fan-out
    g.add_edge("apply_node",                 "fan_out_ad_node")
    g.add_edge("fan_out_ad_node",            "next_usecase_node")

    # Phase 4
    g.add_edge("op_ad_prompt_node",          "op_ad_llm_node")
    g.add_edge("op_ad_llm_node",             "op_ad_parse_node")
    g.add_edge("op_ad_parse_node",           "apply_op_ad_node")

    # Loop
    g.add_conditional_edges("next_usecase_node", route_after_next, {
        "pick_usecase_node": "pick_usecase_node",
        END               : END,
    })

    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn   = sqlite3.connect("RUNTIME_DIR + "/"design_checkpoints.db",
                             check_same_thread=False)
    memory = SqliteSaver(conn)
    return g.compile(
        checkpointer = memory,
    )


graph = build_graph()


if __name__ == "__main__":
    import argparse, json, sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--source",    default="rhapsody",
                        choices=["rhapsody", "excel"])
    parser.add_argument("--excel",     default=None,
                        help="Path to Excel requirements file (used when --source=excel)")
    parser.add_argument("--output",    required=True)
    parser.add_argument("--resume",    default=None,
                        help="JSON resume value for interrupted graph")
    args = parser.parse_args()

    config = {"configurable": {"thread_id": args.component}}
    output_file = args.output

    def write_output(status, **kwargs):
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"status": status, **kwargs}, f, indent=2)

    initial_state = {
        "component_name"    : args.component,
        "requirement_source": args.source,
        "excel_path"        : args.excel or "",
    }

    try:
        if args.resume:
            resume_val = json.loads(args.resume) if args.resume.startswith("{") else args.resume
            print(f"[Resume] Resuming with: {resume_val!r}", file=sys.stderr)

            # Try streaming first to see if graph has pending interrupt
            nodes_run = []
            result = None
            for chunk in graph.stream(Command(resume=resume_val), config, stream_mode="updates"):
                for node_name, node_data in chunk.items():
                    print(f"[Resume] Node: {node_name}", file=sys.stderr)
                    nodes_run.append(node_name)
                    # Only update result if node returned meaningful data
                    if isinstance(node_data, dict) and node_data:
                        result = {**(result or {}), **node_data}

            if not nodes_run:
                # Checkpoint already consumed — read state and apply directly
                print("[Resume] No nodes ran — applying from saved state", file=sys.stderr)
                state = graph.get_state(config)
                if state and state.values:
                    vals = state.values
                    approved_val = resume_val.strip().lower() in ("apply","approve","yes","ok") \
                                   if isinstance(resume_val, str) else False
                    if approved_val:
                        # Run apply logic directly
                        from apply_design import apply_design_state
                        result = apply_design_state(vals, args.component)
                    else:
                        result = {"errors": [], "approved": False}

            errors  = (result or {}).get("errors", [])
            approved = (result or {}).get("approved", False)
            new_ops  = (result or {}).get("new_operations", [])
            op_ads   = (result or {}).get("operation_ads", [])
            print(f"[Resume] approved={approved} new_ops={len(new_ops)} op_ads={len(op_ads)}", file=sys.stderr)
            write_output("completed", success=len(errors)==0,
                        summary=f"Design applied for {args.component}",
                        errors=errors, approved=approved,
                        new_operations=new_ops, operation_ads=op_ads)
        else:
            result = None
            for chunk in graph.stream(initial_state, config, stream_mode="updates"):
                # chunk is {node_name: node_output_dict}
                for node_name, node_data in chunk.items():
                    print(f"[Graph] Node: {node_name}", file=sys.stderr)

                    # Check for interrupt
                    if node_name == "__interrupt__":
                        interrupts = node_data
                        if isinstance(interrupts, (list, tuple)) and len(interrupts) > 0:
                            iv = interrupts[0]
                            interrupt_data = iv.value if hasattr(iv, 'value') else {}
                        else:
                            interrupt_data = {}
                        # Get current state for requirements info
                        try:
                            gstate = graph.get_state(config)
                            state_vals = gstate.values if gstate else {}
                        except:
                            state_vals = {}
                        current_reqs = state_vals.get("current_reqs") or []
                        req_groups   = state_vals.get("req_groups") or {}
                        reqs_considered = []
                        for r in current_reqs:
                            uc = "—"
                            for uc_name, uc_reqs in req_groups.items():
                                if isinstance(uc_reqs, list) and any(
                                    (rr.get("id") if isinstance(rr,dict) else rr) == r.get("id")
                                    for rr in uc_reqs
                                ):
                                    uc = uc_name
                                    break
                            reqs_considered.append({
                                "id"    : r.get("id",""),
                                "text"  : r.get("text","")[:100],
                                "usecase": uc,
                            })
                        write_output(
                            "interrupted",
                            phase            = interrupt_data.get("phase", "human_review"),
                            interrupt_message= interrupt_data.get("message", "Review required"),
                            requirements_considered = reqs_considered,
                            data             = {
                                k: v for k, v in interrupt_data.items()
                                if k != "message"
                            },
                        )
                        sys.exit(0)

                    if isinstance(node_data, dict):
                        result = node_data

            # Completed
            errors = result.get("errors", []) if result else []
            write_output(
                "completed",
                success        = len(errors) == 0,
                summary        = f"Design complete for {args.component}",
                errors         = errors,
                new_operations = result.get("new_operations", []) if result else [],
                operation_ads  = result.get("operation_ads", []) if result else [],
            )

    except Exception as e:
        import traceback
        write_output("error", error=str(e), traceback=traceback.format_exc())
        sys.exit(1)
