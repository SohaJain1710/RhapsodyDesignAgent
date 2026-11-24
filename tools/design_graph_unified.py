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
         → human_review_ad   ◄── HITL #1: approve/correct Analysis AD
         → design_elements_prompt → llm → design_elements_parse
         → design_ibd_prompt → llm → design_ibd_parse
         → op_ad_generate    (Operation ADs, shown in the review's Dynamic View)
         → human_review      ◄── HITL #2: approve full design (ops/intfs/IBD/op ADs)
         → apply
         → fan_out_ad        (create the approved Operation ADs in Rhapsody)
         → next_usecase → (loop or END)
"""

import rhapsody_connect  # connect to the running Rhapsody (repairs COM cache, picks instance with open project)
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
    module_names     : list        # module classes (AB12Module) of the component
    interface_stereotypes: list    # stereotype(s) used by existing interfaces
    interface_pkg    : str         # package that holds existing interfaces
    design_warnings  : list        # validation warnings shown in the design review
    known_interfaces : list        # interfaces that already exist in the component
    ibd_name         : str         # package-level IBD diagram name
    existing_ports   : dict        # {module: {port: {provided, required}}}
    unexposed_ops    : list        # "module.op" public operations not offered by any interface
    module_members   : dict        # {module: {operations: [...], attributes: [...]}}
    interface_ops    : dict        # {existing interface: [operation dicts]}
    ad_only_ops      : list        # existing module ops (implementing an interface) without AD
    op_ad_links      : dict        # {op: {design action id: [analysis action ids]}} from the LLM
    op_ad_feedback   : dict        # {op: reviewer feedback} → regenerate only these Operation ADs
    op_ads_replace   : list        # op names whose AD the reviewer edited (replace existing AD)

    # Shared LLM bus
    current_phase    : str         # which phase is using llm_node
    llm_prompt       : str         # assembled prompt
    llm_response     : str         # raw LLM output

    # Phase outputs
    updated_ad       : str         # Phase 2a
    # Per-use-case proposals: overwritten (not accumulated) so a re-run after
    # reviewer feedback, or the next use case, does not duplicate/re-apply them.
    new_operations   : list        # Phase 2b
    new_interfaces   : list        # Phase 2b
    action_op_map    : dict        # Phase 2b
    bdd_delta        : dict        # Phase 2b
    ibd_delta        : dict        # Phase 3
    operation_ads    : Annotated[list, operator.add]   # Phase 4
    applied_usecases : Annotated[list, operator.add]   # use cases written to Rhapsody

    # Control
    approved         : bool
    human_feedback   : str         # feedback from full design review (HITL #2)
    ad_feedback      : str         # feedback from Analysis AD review (HITL #1)
    op_ads           : dict        # {op_name → mermaid} edited by the reviewer
    errors           : Annotated[list, operator.add]


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
- Reply exactly NO_CHANGE only when all requirements are formally linked AND every visible node label is a meaningful description.
- Labels such as A1, A2, A3, D1, Action1, or node IDs are placeholders, not meaningful descriptions; replace them using the requirements and use-case context.
- Preserve existing node IDs and transition structure when they already express the required behavior, but update placeholder labels even when adding no nodes.
- If existing actions semantically cover the requirements but links are missing or labels are placeholders, return the existing flowchart structure with descriptive labels followed by the full requirements mapping for ALL actions.
- Do NOT add new actions when existing actions already cover the requirements; clarify their visible labels instead.
- Only ADD new abstract actions/decisions when a requirement cannot be covered by any existing action
- Keep meaningful existing action labels unchanged.
- Action labels MUST state the business-level action in plain language; do not expose short codes or opaque IDs as visible text.
- Put every decision question in an action box using action_X["Descriptive question?<br><<analysis>>"]. Connect that action to decision_X{{"decision"}}; the decision diamond's visible label must be exactly "decision" with no question text or stereotype.
- Every decision diamond must have exactly one incoming transition, from its question action. Route guarded outgoing transitions (for example, -->|Yes| and -->|No|) from the decision diamond. When branches converge before a decision, route them to the question action, not directly to the decision. Do not use a merge_X node for this convergence.
- When updating an existing diagram, move any question currently shown inside a decision diamond into a preceding descriptive action box, relabel the diamond exactly "decision", and connect the action to the diamond. Preserve the existing branch meaning and guards.
- Every new action MUST have stereotype <<analysis>>
- Always declare the end node as activity_final([⏹ end]) and connect every terminal path to it.
- Actions describe business-level steps — no implementation vocabulary, no C-code concepts
- MAX ~7 elements per diagram level; group more than that into a named subactivity
- Forbidden in analysis perspective: Call Operations, Activity Parameters, Swimlanes
- Fork/Join is allowed ONLY when the ordering of actions is genuinely unspecified
- Edge labels (transition conditions) MUST NOT use square brackets: write -->|Yes| or -->|Preconditions met|, NEVER -->|[Yes]|
- After the flowchart, list which requirements EACH action covers (both existing and new):

Requirements linked per action:
    <action_id>: <REQ_ID1>, <REQ_ID2>

Return ONLY the Mermaid flowchart followed by the requirements mapping.
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

# Module Classes of this Component (AB12Module)
{modules}

# Public Module Operations NOT Offered by Any Interface
{unexposed_ops}

Rules:
- Reuse existing operations where possible — only add new ones if truly needed
- New operation names: follow existing naming convention from BDD (rb_<componentPrefix>_<VerbNoun>)
- Module names MUST follow: rb_<moduleShortName>_<ModuleName>  (e.g. rb_hvsm_HighVoltageSupplyMonitoring)
- Interface names MUST follow: rb_<moduleShortName>_<InterfaceName>Intf  (e.g. rb_hvsm_ProvideConfigStructureIntf)
- Use existing types from BDD — do NOT invent new types
- For each action in the AD, map it to an existing or new operation
- New functionality: map each new action to exactly ONE module (1 action → 1 module)
- Every new operation MUST name its owning module in "module" — one of the Module Classes above
- A PUBLIC operation that this use case needs and that is called from outside its module
  (another module, the SW component boundary, other components) MUST be offered by an
  interface realized by its module. This applies to NEW operations and to EXISTING public
  operations listed under "Public Module Operations NOT Offered by Any Interface" that are
  used by this use case: propose a new interface (in "new_interfaces", with those
  operations) or add them to a fitting existing interface, plus the realization.
  Operations used only inside their own module stay private and need no interface
- Every operation of an interface must ALSO exist on the module that realizes it (that is
  where it is implemented and its Operation AD is drawn): list those module operations in
  "new_operations" too, unless the module already has them
- Interfaces are realized ONLY by a module class from the Module Classes list above:
  "realized_by" and every realizations "specific" MUST be one of those module names.
  NEVER use the use case name, the SW component, or an actor as realizer.
- Every operation (also inside "new_interfaces") MUST be a JSON object with "name",
  "return_type" and "arguments" — never a signature string

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
      "req_ids": ["SRS_XXX_001"],
      "module": "<module class from the Module Classes list>"
    }}
  ],
  "new_interfaces": [
    {{
      "name": "rb_<moduleShortName>_<InterfaceName>Intf",
      "stereotypes": ["Interface"],
      "operations": [
        {{
          "name": "rb_<prefix>_GetSomething",
          "return_type": "<existing_type>",
          "arguments": [{{"name": "param_name", "type": "<existing_type>"}}]
        }}
      ],
      "attributes": [],
      "realized_by": "<module class from the Module Classes list>"
    }}
  ],
  "realizations": [
    {{"specific": "<module class from the Module Classes list>", "general": "rb_<moduleShortName>_<InterfaceName>Intf"}}
  ]
}}
"""

DESIGN_IBD_PROMPT = """\
You are a SysML architect working on AB12 Static Detailed Design (IBD level).
Propose the IBD changes (ports and connectors) needed for the interfaces of this design step.

# Component
{component}

# Module Classes of this Component (port owners)
{modules}

# Interfaces of this Design Step (interface -> realizing module)
{interfaces}

# Existing Ports per Module (port: +provided -required)
{existing_ports}

# Current IBD (Mermaid)
{ibd}

Rules:
- Every port is owned by a MODULE class from the list above: set "module" to that class name.
  A port on the SW COMPONENT boundary instead gets "boundary": true (and no module) — the module
  may have the same name as the component, so never put a boundary port under "module"
- The module that REALIZES an interface PROVIDES it on one of its ports ("provided")
- A module that USES an interface realized by another module REQUIRES it ("required")
- Reuse an existing port when it fits (same port name, it will get the interface added);
  only add genuinely new ports. Do not repeat ports that already have the interface
- Port names MUST follow: rb_<moduleShortName>_<PortName>Prt  (e.g. rb_hvsm_VoltageRegulatorMonitoringPrt)
- Only use interfaces from "Interfaces of this Design Step"
- Connectors ("links") join two ports. from_obj / to_obj are part names from the IBD
  (e.g. itsRb_...) or module class names; for a port on the SW component boundary use
  the component name "{component}". Only propose connectors that are really needed;
  both ports must exist or be proposed above
- Delegation (component boundary <-> module): from_obj = "{component}" (the SW component),
  to_obj = the module's PART name from the IBD (e.g. itsRb_...), never the module class name
  on both sides. Only expose an interface on the component boundary when it is used
  outside the component; a missing boundary port is created with the module port's interfaces

Respond ONLY with valid JSON:
{{
  "ports": [
    {{
      "name": "rb_<moduleShortName>_<PortName>Prt",
      "module": "<module class from the list>",
      "provided": ["rb_<moduleShortName>_<InterfaceName>Intf"],
      "required": []
    }},
    {{
      "name": "rb_<moduleShortName>_<PortName>Prt",
      "boundary": true,
      "provided": ["rb_<moduleShortName>_<InterfaceName>Intf"],
      "required": []
    }}
  ],
  "links": [
    {{
      "from_obj": "<part name, module class or component>",
      "from_port": "rb_<moduleShortName>_<PortName>Prt",
      "to_obj": "<part name, module class or component>",
      "to_port": "rb_<moduleShortName>_<OtherPortName>Prt"
    }}
  ]
}}
"""

OP_AD_PROMPT = """\
You are a SysML architect generating an AB12 Operation Activity Diagram (AB12-Design perspective in Rhapsody).
This is the DESIGN level — one activity diagram per operation, showing HOW this operation behaves.
It is distinct from the Analysis Activity Diagram, which is given only as context.

# Operation
Module      : {module}
Name        : {op_name}
Arguments   : {arguments}
Return type : {return_type}
Rationale   : {rationale}
Requirements: {req_ids}

# Analysis Action(s) Implemented by this Operation
{implements}

# Actions of the Analysis Activity Diagram (id: text)
{analysis_actions}

# Existing Members of {module} (may be used / called by this operation)
{members}

# Analysis Activity Diagram (context only)
{updated_ad}

Content rules:
- Describe what THIS operation does step by step, based on its signature, rationale,
  requirements and the analysis action(s) it implements
- Use 1 to 5 design actions — abstract design intent, no C code, no variable-level detail
- Refer to existing members of the module by name where the operation uses them
- Show how the return value / result is determined when the return type is not void

Syntax rules (exact):
- The first line MUST be exactly: graph TD
- Start node: initial([▶ start])     End node: activityfinal_1([⏹ end])
- Actions: action_0["Action name<br><<design>>"] with sequential IDs (action_0, action_1, ...)
- Every decision QUESTION goes into an action box: action_2["Is the section valid?<br><<design>>"],
  followed by a decision diamond whose label is exactly "decision": decision_0{{"decision"}}
  Every decision diamond has exactly one incoming transition (from its question action);
  its guarded outgoing transitions use -->|Yes| / -->|No| (no square brackets in labels)
- Merge nodes: merge_0((merge))
- Valid Mermaid identifiers only (letters, digits, underscores); use <br> before the stereotype,
  no raw newlines inside node declarations
- Every action label MUST end with <br><<design>> (never <<analysis>>)
- Do NOT use fork/join nodes, Call Operations or Call Behaviours
- Return ONLY the Mermaid flowchart, then the analysis links block below —
  no Markdown fences, no explanation

Traceability (required): after the flowchart, add one line per design action (also
question actions) naming the analysis action id(s) it refines, from the list above:
Analysis links per action:
action_0: <analysis action id>
action_1: <analysis action id>, <analysis action id>

Syntax example (illustrative only):
graph TD
    initial([▶ start])
    action_0["Read the section status<br><<design>>"]
    action_1["Is the section eligible for the check?<br><<design>>"]
    decision_0{{"decision"}}
    action_2["Calculate and compare the CRC<br><<design>>"]
    merge_0((merge))
    activityfinal_1([⏹ end])
    initial --> action_0
    action_0 --> action_1
    action_1 --> decision_0
    decision_0 -->|Yes| action_2
    decision_0 -->|No| merge_0
    action_2 --> merge_0
    merge_0 --> activityfinal_1
Analysis links per action:
action_0: action_select
action_1: action_eligible
action_2: action_crc
"""


_LINKS_HEADER = "analysis links per action"


def _clean_label(text: str) -> str:
    import re as _r
    t = _r.sub(r"<br\s*/?>", " ", str(text or ""), flags=_r.I)
    t = _r.sub(r"<<\s*(?:design|analysis)\s*>>", "", t, flags=_r.I)
    t = _r.sub(r"<[^>]*>", "", t)
    return " ".join(t.split()).strip().strip('"')


def analysis_actions(updated_ad: str) -> list:
    """[(id, text)] of the action boxes of the Analysis AD (no decisions/merges)."""
    try:
        from mermaid_to_ad import from_mermaid
        plan = from_mermaid(updated_ad or "")
        return [(a["id"], _clean_label(a.get("text") or a.get("name")))
                for a in plan.get("actions", []) if a.get("type", "action") == "action"]
    except Exception:
        return []


_MERMAID_KEYWORDS = ("%%", "classdef", "class ", "style ", "linkstyle", "subgraph", "end",
                     "direction", "click ")


def _is_mermaid_line(t: str) -> bool:
    import re as _r
    low = t.lower()
    if any(x in t for x in ("-->", "---", "-.->", "==>", "-.-")):
        return True
    if low.startswith(_MERMAID_KEYWORDS):
        return True
    # node declaration: id["..."], id(...), id{...}, id((..)), id([..]), id>..]
    return bool(_r.match(r'^\w+\s*(\[|\(|\{|>)', t))


def split_op_ad(text: str):
    """LLM output → (mermaid flowchart, {design action id: [analysis action ids]}).

    Robust against what LLMs add around the diagram: Markdown fences, prose
    before/after, a bold / differently worded links header, or link lines
    without any header. Only real Mermaid lines go into the diagram — any
    leftover non-Mermaid line made the diagram invalid (blank in chat)."""
    import re as _r
    lines = [l for l in (text or "").splitlines() if not l.strip().startswith("```")]
    start = next((i for i, l in enumerate(lines)
                  if _r.match(r'^\s*(graph|flowchart)\b', l, _r.I)), None)
    header = lines[start].strip() if start is not None else "graph TD"
    diagram, links, in_links = [header], {}, False
    for l in lines[(start + 1) if start is not None else 0:]:
        t = l.strip().strip("*").strip()
        if not t:
            continue
        if _r.match(r'^[#*_\s]*(analysis\s+links|traceability|links?\s+to\s+analysis)', t, _r.I):
            in_links = True
            continue
        m = _r.match(r'^[-*\s]*(\w+)\s*(?::|->|=>)\s*([\w\s,]+)$', t)
        if m and not _is_mermaid_line(t.replace("->", "").replace("=>", "")):
            ids = [x.strip() for x in m.group(2).split(",") if x.strip()]
            if ids:
                links[m.group(1)] = ids
            continue
        if in_links:
            continue
        if _is_mermaid_line(t):
            diagram.append(l.rstrip())
        # anything else is prose -> dropped
    return "\n".join(diagram), links


def resolve_action_links(mermaid: str, explicit: dict, op_name: str, state: dict):
    """{design action id: [analysis action TEXT]} for every action of the
    Operation AD: the LLM's links, else the analysis action(s) the operation
    implements (action_op_map). Returns (links, warnings)."""
    from mermaid_to_ad import from_mermaid
    acts = dict(analysis_actions(state.get("updated_ad", "")))
    by_text = {_clean_label(t).lower(): t for t in acts.values()}
    implemented = [by_text.get(_clean_label(a).lower(), _clean_label(a))
                   for a, o in (state.get("action_op_map") or {}).items() if o == op_name]
    out, warnings = {}, []
    try:
        design = [a["id"] for a in from_mermaid(mermaid).get("actions", [])
                  if a.get("type", "action") == "action"]
    except Exception:
        design = []
    for aid in design:
        texts = []
        for x in (explicit or {}).get(aid, []):
            if x in acts:
                texts.append(acts[x])
            elif _clean_label(x).lower() in by_text:
                texts.append(by_text[_clean_label(x).lower()])
        texts = texts or implemented
        if texts:
            out[aid] = list(dict.fromkeys(texts))
        else:
            warnings.append(f"action {aid} has no analysis action to trace to")
    return out, warnings


def build_op_ad_prompt(op: dict, state: dict) -> str:
    """OP_AD_PROMPT for one operation (shared by the graph and apply_design)."""
    name = op.get("name", "")
    implements = [act for act, o in (state.get("action_op_map") or {}).items() if o == name]
    module = op.get("module") or state.get("component_name", "")
    members = (state.get("module_members") or {}).get(module) or {}
    member_lines = [f"- {m}" for m in (members.get("operations", []) + members.get("attributes", []))[:60]]
    return OP_AD_PROMPT.format(
        module      = module,
        op_name     = name,
        arguments   = json.dumps(op.get("arguments", []), indent=2),
        return_type = op.get("return_type", "void"),
        rationale   = op.get("rationale", ""),
        req_ids     = ", ".join(op.get("req_ids", []) or []),
        implements  = "\n".join(f"- {a}" for a in implements) or "(not mapped to a specific analysis action)",
        analysis_actions = "\n".join(f"- {i}: {t}" for i, t in analysis_actions(state.get("updated_ad", "")))
                           or "(none)",
        members     = "\n".join(member_lines) or "(none known)",
        updated_ad  = state.get("updated_ad", ""),
    )


def clean_op_ad(text: str) -> str:
    """LLM output → Mermaid flowchart ready for create_operation_ad."""
    return _sanitize_mermaid(_with_flowchart_header(text or ""))


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
        from read_all_requirements import get_rhapsody, collect_all_requirements

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
                files = glob.glob(os.path.join(RUNTIME_DIR, f"{component_name}*.xlsx")) + \
                        glob.glob(os.path.join(RUNTIME_DIR, "requirements*.xlsx"))
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
            requirements, srs_names = collect_all_requirements(project, component_name)
            if srs_names:
                print(f"[Ingest] Rhapsody requirements: {len(requirements)} from {srs_names}")
                log.step("ingest", f"Requirements read from: {', '.join(srs_names)}")
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
        "new_operations"   : [],
        "new_interfaces"   : [],
        "op_ads"           : {},
        "op_ad_links"      : {},
        "ad_only_ops"      : [],
        "ad_feedback"      : "",
        "human_feedback"   : "",
        "approved"         : False,
        "llm_prompt"       : "",
        "llm_response"     : "",
    }


# ── Read Context ──────────────────────────────────────────────────────────────

_MODULE_STEREOTYPES = ("AB12Module", "SWModuleBlock")
_NON_INTF_STEREOTYPES = set(_MODULE_STEREOTYPES) | {"AB12SWComponent", "AB12ConfigFile"}


def _read_design_structure(project, dd_pkg, comp_pkg, bdds: list):
    """Return (module_names, interface_stereotypes, interface_package_name)."""
    from collections import Counter
    try:
        from project_config import get_conventions
        mod_st = get_conventions(project.name).get("module_stereotype")
    except Exception:
        mod_st = None
    module_sts = set(_MODULE_STEREOTYPES) | ({mod_st} if mod_st else set())

    modules, intf_counter, intf_names = [], Counter(), []
    for bdd in bdds:
        for c in bdd.get("classes", []):
            sts = c.get("stereotypes", []) or []
            if module_sts & set(sts) or c.get("role") == "module":
                if c["name"] not in modules:
                    modules.append(c["name"])
            elif (c["name"].endswith(("Intf", "If"))
                  or any("interface" in st.lower() for st in sts)):
                intf_names.append(c["name"])
                for st in sts:
                    if st not in _NON_INTF_STEREOTYPES:
                        intf_counter[st] += 1

    # Modules not shown on any BDD: walk the DD package
    def walk(pkg, depth=0):
        if pkg is None or depth > 8:
            return
        try:
            for i in range(1, pkg.classes.Count + 1):
                cls = pkg.classes.Item(i)
                try:
                    sts = {cls.stereotypes.Item(k).name for k in range(1, cls.stereotypes.Count + 1)}
                except Exception:
                    sts = set()
                if module_sts & sts and cls.name not in modules:
                    modules.append(cls.name)
        except Exception:
            pass
        try:
            for i in range(1, pkg.packages.Count + 1):
                walk(pkg.packages.Item(i), depth + 1)
        except Exception:
            pass
    walk(dd_pkg)

    intf_pkg = ""
    if intf_names:
        try:
            from create_bdd import find_class_recursive
            cls = find_class_recursive(comp_pkg, intf_names[0])
            if cls is not None:
                intf_pkg = cls.owner.name
        except Exception:
            pass

    stereotypes = [st for st, _ in intf_counter.most_common(1)]
    return modules, stereotypes, intf_pkg, list(dict.fromkeys(intf_names))


def _module_members(bdds: list, module_names: list) -> dict:
    """{module: {"operations": [signatures], "attributes": [name: type]}} from the BDDs."""
    out = {}
    for bdd in bdds:
        for c in bdd.get("classes", []):
            if c["name"] not in module_names or c.get("role") == "component":
                continue
            m = out.setdefault(c["name"], {"operations": [], "attributes": []})
            for op in c.get("operations", []) or []:
                args = ", ".join(f"{a.get('name')}: {a.get('type', '')}" for a in op.get("arguments", []))
                sig = f"{op['name']}({args}) {op.get('return_type') or 'void'}"
                if sig not in m["operations"]:
                    m["operations"].append(sig)
            for at in c.get("attributes", []) or []:
                a = f"{at['name']}: {at.get('type', '')}"
                if a not in m["attributes"]:
                    m["attributes"].append(a)
    return out


def _interface_ops(bdds: list, module_names: list) -> dict:
    """{interface: [operation dicts]} for the interfaces shown on the BDDs."""
    out = {}
    for bdd in bdds:
        for c in bdd.get("classes", []):
            if c["name"] in module_names or c.get("role") in ("component", "module", "config"):
                continue
            if c["name"].endswith(("Intf", "If")) or any(
                    "interface" in st.lower() for st in c.get("stereotypes", []) or []):
                ops = out.setdefault(c["name"], [])
                for op in c.get("operations", []) or []:
                    if all(o["name"] != op["name"] for o in ops):
                        ops.append({k: op.get(k) for k in ("name", "arguments", "return_type")})
    return out


def _unexposed_public_ops(bdds: list, module_names: list) -> list:
    """Public operations of the module classes that no interface on the BDDs offers."""
    offered, public = set(), {}
    for bdd in bdds:
        for c in bdd.get("classes", []):
            ops = c.get("operations", []) or []
            if c["name"] in module_names and c.get("role") != "component":
                for op in ops:
                    if str(op.get("visibility", "public")).lower() == "public":
                        public.setdefault(op["name"], c["name"])
            elif c["name"].endswith(("Intf", "If")) or any(
                    "interface" in st.lower() for st in c.get("stereotypes", []) or []):
                offered |= {op["name"] for op in ops}
    return [f"{mod}.{name}" for name, mod in public.items() if name not in offered]


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

        r  = rhapsody_connect.get_active_rhapsody()
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

        # Find BDD diagram names dynamically — prefer the conventional names
        # (…DetailedDesignBDD / …DetailedDesignInterfacesBDD) over any other OMD
        bdd_dd_name    = None
        bdd_intf_name  = None
        try:
            omd_names = [dd_pkg.objectModelDiagrams.Item(i).name
                         for i in range(1, dd_pkg.objectModelDiagrams.Count + 1)]
            intf_cands = [n for n in omd_names if "Interface" in n or "Intf" in n]
            dd_cands   = [n for n in omd_names if n not in intf_cands]
            bdd_intf_name = next((n for n in intf_cands if n.endswith("DetailedDesignInterfacesBDD")),
                                 intf_cands[0] if intf_cands else None)
            bdd_dd_name   = next((n for n in dd_cands if n.endswith("DetailedDesignBDD")),
                                 next((n for n in dd_cands if "BDD" in n),
                                      dd_cands[0] if dd_cands else None))
        except Exception as e:
            print(f"[Context] Could not list BDDs: {e}")
        print(f"[Context] BDD diagrams: DD={bdd_dd_name}, Intf={bdd_intf_name}")

        # BDDs
        bdds_dd   = read_bdd_from_package(dd_pkg, bdd_dd_name)   if bdd_dd_name   else []
        bdds_intf = read_bdd_from_package(dd_pkg, bdd_intf_name) if bdd_intf_name else []
        unrealized = find_unrealized_interfaces(dd_pkg, comp_pkg)

        bdd_dd_str   = bdd_mermaid(bdds_dd[0])   if bdds_dd   else "%% No DD BDD"

        # Module classes (owners of new operations, realizers of interfaces),
        # existing interface stereotype(s) and the package interfaces live in
        module_names, intf_stereotypes, intf_pkg, intf_names = _read_design_structure(
            p, dd_pkg, comp_pkg, bdds_dd + bdds_intf)
        unexposed = _unexposed_public_ops(bdds_dd + bdds_intf, module_names)
        module_members = _module_members(bdds_dd + bdds_intf, module_names)
        interface_ops  = _interface_ops(bdds_dd + bdds_intf, module_names)
        # which module operations already have an Operation AD
        try:
            from create_operation_ad import _existing_ads
            from create_bdd import find_class_recursive as _fc
            for m in module_names:
                mcls = _fc(dd_pkg, m)
                if mcls is None:
                    continue
                with_ad = []
                for i in range(1, mcls.operations.Count + 1):
                    o = mcls.operations.Item(i)
                    if _existing_ads(o):
                        with_ad.append(o.name)
                module_members.setdefault(m, {"operations": [], "attributes": []})["ops_with_ad"] = with_ad
        except Exception as e:
            print(f"[Context] Could not read existing Operation ADs: {e}")
        print(f"[Context] Public module operations not in any interface: {len(unexposed)}")
        print(f"[Context] Modules: {module_names} | interface stereotypes: "
              f"{intf_stereotypes} | interface package: {intf_pkg or dd_pkg.name}")
        bdd_intf_str = bdd_mermaid(bdds_intf[0],
                           unrealized=unrealized) if bdds_intf else "%% No Interfaces BDD"

        # IBD — failure-safe: a component without IBD must not lose the AD/BDD context
        ibd_str, ibd_name, existing_ports = "%% No IBD", "", {}
        try:
            from read_ibd import find_component_class, module_ports
            from create_bdd import find_class_recursive as _find_cls
            comp_cls = find_component_class(comp_pkg, state["component_name"])
            ibd_data = read_ibd_from_package(
                dd_pkg, comp_cls=comp_cls,
                ibd_names=[f"{m}IBD" for m in module_names] + [f"{state['component_name']}IBD"])
            ibd_str  = ibd_mermaid(ibd_data)
            ibd_name = (ibd_data or {}).get("diagram_name", "")
            for m in module_names:
                mcls = _find_cls(dd_pkg, m)
                if mcls is not None:
                    existing_ports[m] = module_ports(mcls)
            print(f"[Context] IBD: {ibd_name or 'none'} | ports per module: "
                  f"{ {k: len(v) for k, v in existing_ports.items()} }")
        except Exception as e:
            print(f"[Context] IBD read failed (continuing without IBD context): {e}")

        return {
            "existing_ad"    : ad_str,
            "existing_req_map": existing_req_map,
            "bdd_dd"         : bdd_dd_str,
            "bdd_interfaces" : bdd_intf_str,
            "bdd_dd_name"    : bdd_dd_name or "DetailedDesignBDD",
            "bdd_intf_name"  : bdd_intf_name or "DetailedDesignInterfacesBDD",
            "ibd"            : ibd_str,
            "unrealized_intfs": unrealized.get("unrealized", []),
            "module_names"   : module_names,
            "interface_stereotypes": intf_stereotypes,
            "interface_pkg"  : intf_pkg,
            "known_interfaces": list(dict.fromkeys(intf_names + unrealized.get("all_interfaces", []))),
            "unexposed_ops"  : unexposed,
            "module_members" : module_members,
            "interface_ops"  : interface_ops,
            "ibd_name"       : ibd_name,
            "existing_ports" : existing_ports,
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

    prompt = UPDATE_AD_PROMPT.format(
        usecase     = state["current_usecase"],
        requirements= req_text,
        existing_ad = flowchart_only,
        req_links   = req_links,
    )

    # Re-run after the reviewer rejected the previous proposal (HITL #1)
    feedback = (state.get("ad_feedback") or "").strip()
    if feedback:
        prompt += (
            "\n\n# Reviewer Feedback on Your Previous Proposal\n"
            "Your previous proposal was:\n"
            f"{state.get('updated_ad', '')}\n\n"
            f"The reviewer rejected it with this feedback:\n{feedback}\n\n"
            "Revise the diagram to address the feedback. Do NOT reply NO_CHANGE.\n"
        )

    return {
        "current_phase": "update_ad",
        "llm_prompt"  : prompt,
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

    # Sanitize edge labels (strip square brackets that break Mermaid)
    flowchart = _sanitize_mermaid(flowchart)

    # Normalize flowchart keyword
    flowchart = flowchart.replace("flowchart TD", "graph TD")\
                         .replace("flowchart LR", "graph LR")

    print(f"[UpdateAD] Updated AD: {len(flowchart)} chars, total req mappings: {len(merged)}")
    return {
        "updated_ad"      : flowchart,
        "existing_req_map": merged,
    }


# ── Phase 2b: Design Elements ─────────────────────────────────────────────────

def design_elements_prompt_node(state: DesignState) -> dict:
    req_text = "\n".join(f"- {r['id']}: {r.get('text', r.get('shall',''))}"
                         for r in state["current_reqs"])
    prompt = DESIGN_ELEMENTS_PROMPT.format(
        usecase        = state["current_usecase"],
        requirements   = req_text,
        updated_ad     = state["updated_ad"],
        bdd_dd         = state["bdd_dd"],
        bdd_interfaces = state["bdd_interfaces"],
        modules        = "\n".join(f"- {m}" for m in state.get("module_names") or [])
                         or f"- {state['component_name']}  (module list could not be read)",
        unexposed_ops  = "\n".join(f"- {o}" for o in (state.get("unexposed_ops") or [])[:40])
                         or "(none — all public module operations are offered by an interface)",
    )

    # Re-run after the reviewer rejected the previous design (HITL #2)
    feedback = (state.get("human_feedback") or "").strip()
    if feedback:
        prompt += (
            "\n\n# Reviewer Feedback on Your Previous Proposal\n"
            "Previous proposal:\n"
            + json.dumps({
                "new_operations": state.get("new_operations", []),
                "new_interfaces": state.get("new_interfaces", []),
            }, indent=2)
            + f"\n\nThe reviewer rejected it with this feedback:\n{feedback}\n\n"
            "Revise the proposal to address the feedback.\n"
        )

    return {
        "current_phase": "design_elements",
        "llm_prompt"  : prompt,
    }


def design_elements_parse_node(state: DesignState) -> dict:
    from plan_normalize import validate_design
    result    = extract_json(state["llm_response"], default={})
    if not isinstance(result, dict):
        result = {}
    # Normalize string signatures and make every operation owner / interface
    # realizer a real module class (never the use case or the SW component)
    v = validate_design(result.get("new_operations", []),
                        result.get("new_interfaces", []),
                        result.get("realizations", []),
                        state.get("module_names") or [],
                        state["component_name"],
                        state.get("module_members") or {},
                        state.get("interface_ops") or {})
    new_ops, new_intfs = v["new_operations"], v["new_interfaces"]
    for op in new_ops + v["ad_only_operations"]:
        op["usecase"] = state["current_usecase"]
    if extract_json.last_error:
        v["warnings"].insert(0, f"Design step: {extract_json.last_error} - nothing could be "
                                f"proposed. Use 'Regenerate with Feedback' to retry.")
    elif not new_ops and not new_intfs and not v["realizations"]:
        v["warnings"].insert(0, "The LLM proposed no new operations, interfaces or realizations "
                                "(it considers the existing design sufficient). If that is wrong, "
                                "use 'Regenerate with Feedback'.")
    for w in v["warnings"]:
        print(f"[DesignElements] WARNING: {w}")
    print(f"[DesignElements] Ops: {len(new_ops)}, Intfs: {len(new_intfs)}, "
          f"Realizations: {v['realizations']}")
    return {
        "new_operations" : new_ops,
        "new_interfaces" : new_intfs,
        "design_warnings": v["warnings"],
        "ad_only_ops"    : v["ad_only_operations"],
        "action_op_map"  : result.get("action_op_map", {}),
        "bdd_delta"      : {
            "classes"        : new_intfs,
            "realizations"   : v["realizations"],
            "generalizations": [],
        },
    }


# ── Phase 3: Design IBD ───────────────────────────────────────────────────────

def _ibd_interfaces(state) -> list:
    """(interface, realizing module) pairs created or realized in this step."""
    reals = (state.get("bdd_delta") or {}).get("realizations", []) or []
    pairs = [(r["general"], r["specific"]) for r in reals
             if isinstance(r, dict) and r.get("general")]
    for i in state.get("new_interfaces", []) or []:
        if not any(g == i.get("name") for g, _ in pairs):
            pairs.append((i.get("name"), i.get("realized_by") or ""))
    return pairs


def design_ibd_prompt_node(state: DesignState) -> dict:
    pairs = _ibd_interfaces(state)
    if not pairs:
        # Skip IBD if no interface is created or realized in this step
        return {
            "current_phase": "design_ibd_skip",
            "llm_prompt"  : "",
            "ibd_delta"   : {"ports": [], "links": []},
        }
    existing = state.get("existing_ports") or {}
    ex_lines = []
    for mod, ports in existing.items():
        ex_lines.append(f"{mod}:")
        for pn, c in ports.items():
            contract = " ".join([f"+{x}" for x in c.get("provided", [])] +
                                [f"-{x}" for x in c.get("required", [])])
            ex_lines.append(f"  - {pn}: {contract or '(no interfaces)'}")
    return {
        "current_phase": "design_ibd",
        "llm_prompt"  : DESIGN_IBD_PROMPT.format(
            component      = state["component_name"],
            modules        = "\n".join(f"- {m}" for m in state.get("module_names") or [])
                             or f"- {state['component_name']}",
            interfaces     = "\n".join(f"- {g} -> realized by {m or '(not set)'}" for g, m in pairs),
            existing_ports = "\n".join(ex_lines) or "(none)",
            ibd            = state.get("ibd") or "%% No IBD",
        ),
    }


def design_ibd_parse_node(state: DesignState) -> dict:
    if state.get("current_phase") == "design_ibd_skip":
        return {}
    from plan_normalize import validate_ibd
    result = extract_json(state["llm_response"], default={"ports": [], "links": []})
    parse_err = extract_json.last_error
    v = validate_ibd(result, state.get("module_names") or [], state["component_name"],
                     (state.get("bdd_delta") or {}).get("realizations", []),
                     list(state.get("known_interfaces") or [])
                     + [i.get("name") for i in state.get("new_interfaces", []) or []],
                     state.get("existing_ports") or {})
    if parse_err:
        v["warnings"].insert(0, f"IBD step: {parse_err} - no ports proposed.")
    elif not v["ports"] and not v["links"]:
        v["warnings"].insert(0, "IBD: no new ports/connectors needed (all interfaces of this step "
                                "already have ports with these contracts, or none were proposed).")
    for w in v["warnings"]:
        print(f"[DesignIBD] WARNING: {w}")
    print(f"[DesignIBD] Ports: {len(v['ports'])}, Links: {len(v['links'])}")
    return {"ibd_delta"      : {"ports": v["ports"], "links": v["links"]},
            "design_warnings": list(state.get("design_warnings") or []) + v["warnings"]}


# ── Human Review — helpers ────────────────────────────────────────────────────

_APPROVE_WORDS = ("approve", "approved", "apply", "yes", "ok", "continue")


def _clean_mermaid_flowchart(text: str) -> str:
    """Strip fences, sanitize edge labels and normalize to 'graph TD/LR'."""
    lines = [l for l in (text or "").strip().splitlines()
             if not l.strip().startswith("```")]
    flow = _sanitize_mermaid("\n".join(lines).strip())
    return flow.replace("flowchart TD", "graph TD").replace("flowchart LR", "graph LR")


def _looks_like_flowchart(text: str) -> bool:
    body = "\n".join(l for l in (text or "").strip().splitlines()
                     if not l.strip().startswith("```")).lstrip()
    return body.startswith(("flowchart", "graph"))


# ── Human Review — Analysis AD (HITL #1) ─────────────────────────────────────

def human_review_ad_node(state: DesignState) -> dict:
    """Pause after the Analysis AD update so the human can approve or correct it
    before the pipeline continues to design operations, interfaces, and IBD.

    Accepted resume values:
      "approve" / "apply" / ...         → approve the AD as proposed
      "<mermaid flowchart>"             → approve the reviewer's corrected AD
      "<free text>"                     → reject; regenerate the AD with this feedback
      {"approved": true,  "updated_ad": "...", "req_map": {...}}  → approve (optionally edited)
      {"approved": false, "feedback": "..."}                       → reject with feedback
    """
    print(f"\n[HumanReviewAD] Use case: {state['current_usecase']}")
    print(f"  Updated AD length: {len(state.get('updated_ad',''))}")

    feedback = interrupt({
        "phase"      : "ad_review",
        "component"  : state["component_name"],
        "usecase"    : state["current_usecase"],
        "updated_ad" : state.get("updated_ad", ""),
        "req_map"    : state.get("existing_req_map", {}),
        "message"    : (
            "Review the updated Analysis Activity Diagram. "
            "Approve it, edit it, or give feedback to regenerate it."
        ),
    })

    approved, corrected_ad, req_map, fb_text = False, "", None, ""
    if isinstance(feedback, str):
        text = feedback.strip()
        if not text or text.lower() in _APPROVE_WORDS:
            approved = True
        elif _looks_like_flowchart(text):
            approved, corrected_ad = True, text
        else:
            fb_text = text
    elif isinstance(feedback, dict):
        approved     = bool(feedback.get("approved", False))
        corrected_ad = (feedback.get("updated_ad") or "").strip()
        req_map      = feedback.get("req_map")
        fb_text      = (feedback.get("feedback") or "").strip()
        if not approved and not fb_text and corrected_ad:
            approved = True          # an edited AD without feedback = accept edits
    else:
        fb_text = str(feedback or "")

    if not approved and not fb_text:
        fb_text = "The reviewer rejected the diagram. Re-check it against the requirements."

    result: dict = {
        "approved"      : approved,
        "ad_feedback"   : "" if approved else fb_text,
        "human_feedback": "",
    }
    if approved and corrected_ad:
        result["updated_ad"] = _clean_mermaid_flowchart(corrected_ad)
        print(f"[HumanReviewAD] Reviewer supplied corrected AD ({len(result['updated_ad'])} chars)")
    if approved and isinstance(req_map, dict) and req_map:
        result["existing_req_map"] = req_map
    print(f"[HumanReviewAD] approved={approved} feedback={fb_text[:80]!r}")
    return result


def route_after_ad_review(state: DesignState) -> str:
    if state.get("approved"):
        return "design_elements_prompt_node"
    return "update_ad_prompt_node"   # regenerate the AD using state['ad_feedback']


# ── Human Review — Full Design (HITL #2) ─────────────────────────────────────

def human_review_node(state: DesignState) -> dict:
    print(f"\n[HumanReview] Use case: {state['current_usecase']}")
    print(f"  New ops  : {len(state.get('new_operations',[]))}")
    print(f"  New intfs: {len(state.get('new_interfaces',[]))}")
    print(f"  IBD ports: {len(state.get('ibd_delta',{}).get('ports',[]))}")

    feedback = interrupt({
        "phase"         : "human_review",
        "component"     : state["component_name"],
        "usecase"       : state["current_usecase"],
        "updated_ad"    : state.get("updated_ad", ""),
        "req_map"       : state.get("existing_req_map", {}),
        "new_operations": state.get("new_operations", []),
        "new_interfaces": state.get("new_interfaces", []),
        "realizations"  : (state.get("bdd_delta") or {}).get("realizations", []),
        "modules"       : state.get("module_names") or [],
        "known_interfaces": state.get("known_interfaces") or [],
        "warnings"      : state.get("design_warnings") or [],
        "op_ads"        : state.get("op_ads") or {},
        "ad_only_ops"   : state.get("ad_only_ops") or [],
        "op_ad_links"   : state.get("op_ad_links") or {},
        "ibd_delta"     : state.get("ibd_delta", {}),
        "message"       : "Review the proposed design changes. Reply 'apply' to approve or provide feedback.",
    })

    result: dict = {}
    op_ad_fb: dict = {}
    if isinstance(feedback, str):
        # chat: "opad <OperationName>: <feedback>" regenerates only that Operation AD
        import re as _re
        for m in _re.finditer(r"(?im)^\s*opad\s+(\w+)\s*:\s*(.+?)(?=^\s*opad\s+\w+\s*:|\Z)",
                              feedback.strip(), flags=_re.S):
            op_ad_fb[m.group(1)] = " ".join(m.group(2).split())
        approved = not op_ad_fb and feedback.strip().lower() in _APPROVE_WORDS
        fb_text  = "" if (approved or op_ad_fb) else feedback.strip()
    elif isinstance(feedback, dict):
        approved = bool(feedback.get("approved", False))
        fb_text  = (feedback.get("feedback") or "").strip()
        op_ad_fb = {k: str(v).strip() for k, v in (feedback.get("op_ad_feedback") or {}).items()
                    if str(v).strip()}
        if op_ad_fb:
            approved = False
        edits    = feedback.get("edits")
        if (approved or op_ad_fb) and isinstance(edits, dict):
            # Reviewer edited the design in the webview — use the edited version
            from apply_design import normalize_edits
            norm = normalize_edits(edits, state["component_name"],
                                   usecase=state["current_usecase"],
                                   module_names=state.get("module_names") or [],
                                   known_interfaces=state.get("known_interfaces") or [],
                                   existing_ports=state.get("existing_ports") or {},
                                   module_members=state.get("module_members") or {},
                                   interface_ops=state.get("interface_ops") or {})
            result.update({
                "new_operations": norm["new_operations"],
                "new_interfaces": norm["new_interfaces"],
                "bdd_delta"     : norm["bdd_delta"],
                "ibd_delta"     : norm["ibd_delta"],
                "op_ads"        : norm["op_ads"],
                # keep earlier reviewer-driven ADs (feedback rounds) + newly edited ones
                "op_ads_replace": list(dict.fromkeys(
                    [n for n in (state.get("op_ads_replace") or []) if n in norm["op_ads"]]
                    + [n for n in (edits.get("op_ads_edited") or []) if n in norm["op_ads"]])),
                "ad_only_ops"   : norm.get("ad_only_ops", []),
                "op_ad_links"   : norm.get("op_ad_links", {}) or state.get("op_ad_links") or {},
            })
            if norm.get("updated_ad"):
                result["updated_ad"] = _clean_mermaid_flowchart(norm["updated_ad"])
            if norm.get("req_map"):
                result["existing_req_map"] = norm["req_map"]
            for w in norm.get("warnings", []):
                print(f"[HumanReview] WARNING: {w}")
            result["design_warnings"] = norm.get("warnings", [])
            print(f"[HumanReview] Applying reviewer edits: "
                  f"{len(norm['new_operations'])} ops, {len(norm['new_interfaces'])} intfs")
    else:
        approved = False
        fb_text  = str(feedback)

    if op_ad_fb:
        print(f"[HumanReview] Operation AD feedback for: {list(op_ad_fb)}")
        result.update({"approved": False, "human_feedback": "", "op_ad_feedback": op_ad_fb})
        return result
    if not approved and not fb_text:
        fb_text = "The reviewer rejected the proposal. Re-check it against the requirements."

    result.update({
        "approved"      : approved,
        "human_feedback": fb_text,
        "op_ad_feedback": {},
    })
    return result


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

        r = rhapsody_connect.get_active_rhapsody()
        print(f"[Apply] Connected to Rhapsody: {r.activeProject().name}", file=sys.stderr)

        # ── Approved Analysis AD (HITL #1) → write back to Rhapsody ──────────
        updated_ad = state.get("updated_ad", "")
        if updated_ad and updated_ad.strip():
            try:
                from create_activity_diagram import create_or_update_ad
                ad_res = create_or_update_ad(state["component_name"], state["current_usecase"],
                                             updated_ad, r,
                                             req_map=state.get("existing_req_map", {}))
                print(f"[Apply] Analysis AD update: {ad_res}", file=sys.stderr)
                if ad_res.get("errors"):
                    errors.extend(ad_res["errors"])
            except Exception as e:
                print(f"[Apply] Analysis AD error: {e}", file=sys.stderr)
                errors.append(f"analysis_ad: {e}")

        # ── New operations → their module class; interfaces + realizations ──
        from plan_normalize import build_bdd_plans
        bdd = state.get("bdd_delta") or {}
        op_plans, intf_plan = build_bdd_plans(
            state["component_name"], state.get("new_operations", []),
            bdd.get("classes", []), bdd.get("realizations", []),
            state.get("interface_stereotypes"), state.get("interface_pkg", ""))
        print(f"[Apply] New ops: {len(state.get('new_operations', []))} "
              f"in modules {[p['classes'][0]['name'] for p in op_plans]}", file=sys.stderr)
        for plan in op_plans:
            res = create_bdd_from_plan(plan, r,
                                       diagram_name=state.get("bdd_dd_name","DetailedDesignBDD"))
            print(f"[Apply] create_bdd_from_plan result: {res}", file=sys.stderr)
            errors.extend(res.get("errors") or ([res["error"]] if res.get("error") else []))
        if intf_plan["classes"] or intf_plan["realizations"]:
            res = create_bdd_from_plan(intf_plan, r,
                diagram_name=state.get("bdd_intf_name","DetailedDesignInterfacesBDD"))
            print(f"[Apply] interfaces result: {res}", file=sys.stderr)
            errors.extend(res.get("errors") or ([res["error"]] if res.get("error") else []))

        # IBD delta (ports and/or links)
        ibd = state.get("ibd_delta", {}) or {}
        if ibd.get("ports") or ibd.get("links"):
            res = create_ibd({
                "component_name": state["component_name"],
                "ibd_name"      : state.get("ibd_name", ""),
                "module_names"  : state.get("module_names") or [],
                "ports"         : ibd.get("ports", []),
                "links"         : ibd.get("links", []),
            }, r)
            print(f"[Apply] IBD result: {res}", file=sys.stderr)
            errors.extend(res.get("errors") or ([res["error"]] if res.get("error") else []))

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Apply] EXCEPTION: {e}", file=sys.stderr)
        print(tb, file=sys.stderr)
        errors.append(f"apply_node: {e}\n{tb}")

    # Operation ADs are generated by fan_out_ad_node (runs right after this
    # node) — not here, otherwise every operation AD would be created twice.
    print(f"[Apply] Done. Errors: {len(errors)}", file=sys.stderr)
    return {"applied_usecases": [state["current_usecase"]], "errors": errors}


# ── Operation ADs: generated BEFORE the design review ─────────────────────────

def op_ad_generate_node(state: DesignState) -> dict:
    """Generate the Operation AD of every new operation so the reviewer sees
    (and can edit) the real diagrams in the review's Dynamic View."""
    ops = [op for op in (state.get("new_operations", []) or []) + (state.get("ad_only_ops") or [])
           if op.get("usecase", state["current_usecase"]) == state["current_usecase"]]
    if not ops:
        return {"op_ads": {}, "op_ad_feedback": {}}
    feedback = state.get("op_ad_feedback") or {}
    targeted = bool(feedback)
    op_ads = dict(state.get("op_ads") or {}) if targeted else {}
    op_links = dict(state.get("op_ad_links") or {}) if targeted else {}
    replace = list(state.get("op_ads_replace") or [])
    warnings = []
    unknown = [n for n in feedback if n not in {o.get("name") for o in ops}]
    for n in unknown:
        warnings.append(f"Operation AD feedback for '{n}' ignored - no such operation in this review.")
    for op in ops:
        name = op.get("name", "")
        if targeted and name not in feedback:
            continue                       # keep the other diagrams as they are
        try:
            prompt = build_op_ad_prompt(op, state)
            if targeted:
                prompt += (
                    "\n\n# Reviewer Feedback on Your Previous Diagram\n"
                    f"Previous diagram:\n{op_ads.get(name, '')}\n\n"
                    f"Feedback:\n{feedback[name]}\n\n"
                    "Revise the diagram to address the feedback (same syntax and traceability rules).\n")
            print(f"[OpAD] {'Regenerating' if targeted else 'Generating'} AD for: {name}")
            raw = llm_call(prompt)
            try:
                from debug_logger import DebugLogger
                _log = DebugLogger(state["component_name"])
                _log.llm_prompt(f"op_ad:{name}", prompt)
                _log.llm_response(f"op_ad:{name}", raw)
            except Exception:
                pass
            mm, links = split_op_ad(raw)
            op_ads[name] = clean_op_ad(mm)
            op_links[name] = links
            if targeted and name not in replace:
                replace.append(name)       # reviewer-driven diagram replaces an existing AD
        except Exception as e:
            warnings.append(f"Operation AD for {name} could not be generated: {e} - "
                            f"it will be generated again after Apply.")
    for w in warnings:
        print(f"[OpAD] WARNING: {w}")
    return {"op_ads": op_ads, "op_ad_links": op_links, "op_ad_feedback": {},
            "op_ads_replace": replace,
            "design_warnings": list(dict.fromkeys(list(state.get("design_warnings") or []) + warnings))}


# ── Fan-out: create Operation ADs in Rhapsody (after apply) ───────────────────

def fan_out_ad_node(state: DesignState):
    """Create the Operation AD of each new operation on its module's operation.
    Uses the AD shown/edited in the review; generates one only if missing."""
    ops = [op for op in (state.get("new_operations", []) or []) + (state.get("ad_only_ops") or [])
           if op.get("usecase") == state["current_usecase"]]
    if not ops or not state.get("approved"):
        print("[FanOut] No ops to generate ADs for")
        return {}

    from create_operation_ad import create_operation_ad
    op_ads  = state.get("op_ads") or {}
    replace = set(state.get("op_ads_replace") or [])
    results, errors = [], []
    try:
        r = rhapsody_connect.get_active_rhapsody()
    except Exception as e:
        return {"errors": [f"Operation ADs: {e}"]}

    for op in ops:
        name = op.get("name", "")
        try:
            mermaid = op_ads.get(name) or ""
            explicit = (state.get("op_ad_links") or {}).get(name, {})
            if not mermaid.strip():
                print(f"[FanOut] Generating AD for: {name}")
                mermaid, explicit = split_op_ad(llm_call(build_op_ad_prompt(op, state)))
            mermaid = clean_op_ad(mermaid)
            links, link_warn = resolve_action_links(mermaid, explicit, name, state)
            res = create_operation_ad(state["component_name"], name, mermaid, r,
                                      module=op.get("module"), replace=name in replace,
                                      analysis_links=links, usecase=state["current_usecase"])
            res.setdefault("errors", []).extend(link_warn)
        except Exception as e:
            res = {"success": False, "errors": [f"{e}"]}
        print(f"[FanOut] {name}: {res}")
        results.append({"operation": name, "result": res})
        for err in res.get("errors", []) or ([res["error"]] if res.get("error") else []):
            errors.append(f"Operation AD {name}: {err}")

    return {"operation_ads": results, "errors": errors, "op_ads": {}, "op_ads_replace": [],
            "op_ad_links": {}, "ad_only_ops": []}


def _with_flowchart_header(text: str) -> str:
    """Prepend 'flowchart TD' unless the LLM already returned a full diagram."""
    body = "\n".join(l for l in (text or "").strip().splitlines()
                     if not l.strip().startswith("```")).strip()
    return body if body.startswith(("flowchart", "graph")) else "flowchart TD\n" + body


def _sanitize_mermaid(text: str) -> str:
    """Remove square brackets from edge labels: -->|[Yes]| → -->|Yes|"""
    import re
    return re.sub(r'\|\[([^\]]*)\]\|', r'|\1|', text)


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


def route_after_review(state: DesignState) -> str:
    if state.get("approved"):
        return "apply_node"
    if state.get("op_ad_feedback"):
        return "op_ad_generate_node"        # regenerate only the commented Operation ADs
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
    g.add_node("human_review_ad_node",       human_review_ad_node)

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
    g.add_node("op_ad_generate_node",        op_ad_generate_node)
    g.add_node("fan_out_ad_node",            fan_out_ad_node)

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
    g.add_edge("update_ad_parse_node",       "human_review_ad_node")
    g.add_conditional_edges("human_review_ad_node", route_after_ad_review, {
        "design_elements_prompt_node": "design_elements_prompt_node",
        "update_ad_prompt_node"      : "update_ad_prompt_node",
    })

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
    g.add_edge("design_ibd_parse_node",      "op_ad_generate_node")
    g.add_edge("op_ad_generate_node",        "human_review_node")

    # Review
    g.add_conditional_edges("human_review_node", route_after_review, {
        "apply_node"                 : "apply_node",
        "op_ad_generate_node"        : "op_ad_generate_node",
        "design_elements_prompt_node": "design_elements_prompt_node",
    })

    # Apply + fan-out
    g.add_edge("apply_node",                 "fan_out_ad_node")
    g.add_edge("fan_out_ad_node",            "next_usecase_node")


    # Loop
    g.add_conditional_edges("next_usecase_node", route_after_next, {
        "pick_usecase_node": "pick_usecase_node",
        END               : END,
    })

    return g.compile(checkpointer=_make_checkpointer())


# ── Checkpointer ──────────────────────────────────────────────────────────────
# Each run / resume is a SEPARATE Python process (spawned by the VS Code
# extension), so the checkpoint MUST be persisted to disk — an in-memory saver
# loses the paused graph as soon as the process exits and resume finds nothing.

CHECKPOINT_DB = os.path.join(RUNTIME_DIR, "design_checkpoints.db")


def _make_checkpointer():
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver   # pip: langgraph-checkpoint-sqlite
        conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
        return SqliteSaver(conn)
    except ImportError:
        print("[Checkpoint] langgraph-checkpoint-sqlite not installed — using file-backed "
              "fallback. Run: pip install langgraph-checkpoint-sqlite", file=sys.stderr)
        return _FileBackedSaver(CHECKPOINT_DB + ".pkl")


try:
    from langgraph.checkpoint.memory import InMemorySaver as _MemSaverBase
except ImportError:   # older langgraph
    from langgraph.checkpoint.memory import MemorySaver as _MemSaverBase


class _FileBackedSaver(_MemSaverBase):
    """In-memory saver that is loaded from / flushed to a pickle file after every write."""

    def __init__(self, path: str):
        import threading
        super().__init__()
        self._path = path
        self._lock = threading.RLock()   # langgraph may write from worker threads
        self._load()

    def _load(self):
        import pickle
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "rb") as f:
                data = pickle.load(f)
            for thread, ns_map in data.get("storage", {}).items():
                for ns, ckpts in ns_map.items():
                    self.storage[thread][ns].update(ckpts)
            for k, v in data.get("writes", {}).items():
                self.writes[k].update(v)
            self.blobs.update(data.get("blobs", {}))
        except Exception as e:
            print(f"[Checkpoint] Could not load {self._path}: {e}", file=sys.stderr)

    def _flush(self):
        import pickle
        data = {
            "storage": {t: {ns: dict(c) for ns, c in m.items()} for t, m in self.storage.items()},
            "writes" : {k: dict(v) for k, v in self.writes.items()},
            "blobs"  : dict(self.blobs),
        }
        tmp = self._path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, self._path)

    def put(self, *a, **kw):
        with self._lock:
            r = super().put(*a, **kw); self._flush(); return r

    def put_writes(self, *a, **kw):
        with self._lock:
            r = super().put_writes(*a, **kw); self._flush(); return r

    def delete_thread(self, *a, **kw):
        with self._lock:
            r = super().delete_thread(*a, **kw); self._flush(); return r


graph = build_graph()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _requirements_considered(state_vals: dict) -> list:
    current_reqs = state_vals.get("current_reqs") or []
    req_groups   = state_vals.get("req_groups") or {}
    out = []
    for r in current_reqs:
        uc = "—"
        for uc_name, uc_reqs in req_groups.items():
            if isinstance(uc_reqs, list) and any(
                (rr.get("id") if isinstance(rr, dict) else rr) == r.get("id")
                for rr in uc_reqs
            ):
                uc = uc_name
                break
        out.append({"id": r.get("id", ""), "text": r.get("text", "")[:100], "usecase": uc})
    return out


def _run_graph(graph_input, config, write_output, component: str, label: str) -> None:
    """Stream the graph until it pauses (interrupt) or finishes, then write output.
    Used for BOTH the initial run and every resume, so a resume that reaches the
    next human-review gate is reported as 'interrupted', not as 'completed'."""
    errors, new_ops, op_ads, approved, applied = [], [], [], None, []

    # Consume the WHOLE stream (never return/exit from inside it): the final
    # checkpoint is only guaranteed to be saved once the stream has finished,
    # and resume needs it. durability="sync" saves each step before moving on.
    interrupt_data = None
    try:
        stream = graph.stream(graph_input, config, stream_mode="updates", durability="sync")
    except TypeError:   # older langgraph without the durability option
        stream = graph.stream(graph_input, config, stream_mode="updates")

    for chunk in stream:
        for node_name, node_data in chunk.items():
            print(f"[{label}] Node: {node_name}", file=sys.stderr)

            if node_name == "__interrupt__":
                iv = node_data[0] if isinstance(node_data, (list, tuple)) and node_data else None
                interrupt_data = getattr(iv, "value", None) or {}
                continue

            if isinstance(node_data, dict):
                errors  += node_data.get("errors", []) or []
                op_ads  += node_data.get("operation_ads", []) or []
                if node_data.get("new_operations"):
                    new_ops = node_data["new_operations"]
                if "approved" in node_data:
                    approved = node_data["approved"]
                applied += node_data.get("applied_usecases", []) or []

    if interrupt_data is not None:
        try:
            gstate = graph.get_state(config)
            state_vals = gstate.values if gstate else {}
        except Exception:
            state_vals = {}
        write_output(
            "interrupted",
            phase                   = interrupt_data.get("phase", "human_review"),
            interrupt_message       = interrupt_data.get("message", "Review required"),
            requirements_considered = _requirements_considered(state_vals),
            errors_so_far           = errors,
            applied_usecases        = applied,
            data                    = {k: v for k, v in interrupt_data.items()
                                       if k != "message"},
        )
        return

    write_output(
        "completed",
        success        = len(errors) == 0,
        summary        = (f"Design applied for {component} (use cases: {', '.join(applied)})" if applied
                          else f"Design run finished for {component} (nothing applied)"),
        applied_usecases = applied,
        errors         = errors,
        approved       = approved,
        new_operations = new_ops,
        operation_ads  = op_ads,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    parser.add_argument("--source",    default="rhapsody",
                        choices=["rhapsody", "excel"])
    parser.add_argument("--excel",     default=None,
                        help="Path to Excel requirements file (used when --source=excel)")
    parser.add_argument("--output",    required=True)
    parser.add_argument("--resume",    default=None,
                        help="Resume value (plain text or JSON) for an interrupted graph")
    parser.add_argument("--resume-file", default=None,
                        help="Path to a JSON file holding the resume value (for large edits)")
    args = parser.parse_args()

    config = {"configurable": {"thread_id": args.component}}
    output_file = args.output

    def write_output(status, **kwargs):
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"status": status, **kwargs}, f, indent=2)

    try:
        resume_val = None
        if args.resume_file:
            with open(args.resume_file, encoding="utf-8") as f:
                resume_val = json.load(f)
        elif args.resume is not None:
            txt = args.resume.strip()
            try:
                resume_val = json.loads(txt) if txt.startswith("{") else txt
            except json.JSONDecodeError:
                resume_val = txt

        if resume_val is not None:
            print(f"[Resume] Resuming with: {str(resume_val)[:200]!r}", file=sys.stderr)
            snapshot = graph.get_state(config)
            if not snapshot or not snapshot.next:
                write_output(
                    "error",
                    error=(f"No paused design run found for {args.component}. "
                           f"Start a new run with /design {args.component}."),
                )
                sys.exit(0)
            # Re-supply the component name with the resume (from main's key_error fix).
            # Only this key: a resumed run is always passed --source rhapsody, which
            # must not overwrite the source of an Excel run.
            _run_graph(Command(resume=resume_val, update={"component_name": args.component}),
                       config, write_output,
                       args.component, "Resume")
        else:
            # Fresh run — drop any stale paused run for this component
            try:
                graph.checkpointer.delete_thread(args.component)
            except Exception as e:
                print(f"[Graph] Could not clear old checkpoint: {e}", file=sys.stderr)
            initial_state = {
                "component_name"    : args.component,
                "requirement_source": args.source,
                "excel_path"        : args.excel or "",
            }
            _run_graph(initial_state, config, write_output, args.component, "Graph")

    except Exception as e:
        import traceback
        write_output("error", error=str(e), traceback=traceback.format_exc())
        sys.exit(1)
