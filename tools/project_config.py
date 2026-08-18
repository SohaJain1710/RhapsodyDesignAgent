"""
project_config.py
-----------------
Auto-detects Rhapsody project and returns the correct conventions.
Supports AB15 (AB15_CompleteModel) and AB12 (AB12SWArchitecture).
"""

# ── Convention definitions ─────────────────────────────────────────────────────

CONVENTIONS = {
    "AB15": {
        "project_keywords"    : ["AB15"],
        "component_prefix"    : "rbh_",
        "module_prefix"       : "rbh_",
        "dd_suffix"           : "DetailedDesign",
        "module_stereotype"   : "SWModuleBlock",
        "interface_suffix"    : "If",
        "config_suffix"       : "_Cfg",
        "ad_naming"           : "{class_name}AnalysisAD",
        "design_ad_naming"    : "{operation_name}AD",
        "bdd_naming"          : "{short_name}_DetailedDesignBDD",
        "ibd_naming"          : "{module_name}IBD",
        "swimlanes_in_analysis": True,
        "fork_join_in_analysis": True,
        "analysis_stereotype" : None,
        "max_elements_per_ad" : None,
        "callops_in_analysis" : True,
    },
    "AB12": {
        "project_keywords"     : ["AB12"],
        "component_prefix"     : "rb_",
        "module_prefix"        : "rb_",
        "dd_suffix"            : "DetailedDesign",
        "dd_search_mode"       : "contains",        # search for *DetailedDesign* inside component
        "module_stereotype"    : "AB12Module",
        "interface_suffix"     : "If",
        "config_suffix"        : "_Cfg",
        "ad_naming"            : "{use_case_name}AD",
        "design_ad_naming"     : "{operation_name}AD",
        "bdd_naming"           : "rb_{short_name}_DetailedDesignBDD",
        "ibd_naming"           : "{module_name}IBD",
        "swimlanes_in_analysis": False,
        "fork_join_in_analysis": False,
        "analysis_stereotype"  : "analysis",
        "max_elements_per_ad"  : 9,
        "callops_in_analysis"  : False,
        "sw_model_name"        : None,
        "search_entire_project": True,
        "skip_roots"           : [
            "SysML", "SoftwareArchitectC",
            "AB12SWArchProfile", "AB12Tools",
            "AB12Perspectives", "AB12Analytics",
            "AB12DSWITProfile", "AB12Requirements_v2",
            "AB12Requirements", "CustRequirements",
        ],
    },
    "DEFAULT": {
        "project_keywords"    : [],
        "component_prefix"    : "rb_",
        "module_prefix"       : "rb_",
        "dd_suffix"           : "DetailedDesign",
        "module_stereotype"   : None,
        "interface_suffix"    : "If",
        "config_suffix"       : "_Cfg",
        "ad_naming"           : "{class_name}AnalysisAD",
        "design_ad_naming"    : "{operation_name}AD",
        "bdd_naming"          : "{short_name}_DetailedDesignBDD",
        "ibd_naming"          : "{module_name}IBD",
        "swimlanes_in_analysis": True,
        "fork_join_in_analysis": True,
        "analysis_stereotype" : None,
        "max_elements_per_ad" : None,
        "callops_in_analysis" : True,
    }
}


def detect_project(project_name: str) -> str:
    """Detect project type from project name."""
    name_upper = project_name.upper()
    for key, conv in CONVENTIONS.items():
        if key == "DEFAULT":
            continue
        for keyword in conv["project_keywords"]:
            if keyword.upper() in name_upper:
                import sys; print(f"[ProjectConfig] Detected: {key} (from '{project_name}')", file=sys.stderr)
                return key
    import sys; print(f"[ProjectConfig] Unknown project '{project_name}' — using DEFAULT", file=sys.stderr)
    return "DEFAULT"


def get_conventions(project_name: str) -> dict:
    """Get conventions for a project name."""
    key = detect_project(project_name)
    return {**CONVENTIONS[key], "project_type": key}


def get_conventions_from_rhapsody() -> dict:
    """Connect to Rhapsody and auto-detect conventions."""
    import win32com.client
    try:
        rhapsody     = win32com.client.GetActiveObject("Rhapsody2.Application")
        project      = rhapsody.activeProject()
        project_name = project.name if project else "Unknown"
        import sys; print(f"[ProjectConfig] Project: {project_name}", file=sys.stderr)
        return get_conventions(project_name)
    except Exception as e:
        print(f"[ProjectConfig] Could not connect to Rhapsody: {e}")
        return get_conventions("DEFAULT")


def is_module_class(cls_name: str, stereotype: str, conventions: dict) -> bool:
    """Check if a class is a module (not interface or config)."""
    module_stereo = conventions.get("module_stereotype")
    iface_suffix  = conventions.get("interface_suffix", "If")
    config_suffix = conventions.get("config_suffix", "_Cfg")

    # Stereotype-based check (most reliable)
    if stereotype == "Interface":
        return False
    if stereotype in ("AB12ConfigFile", "ConfigFile"):
        return False
    if module_stereo and stereotype:
        return stereotype == module_stereo

    # Name-based fallback
    is_interface = cls_name.endswith(iface_suffix)
    is_config    = cls_name.endswith(config_suffix)
    return not is_interface and not is_config


if __name__ == "__main__":
    # Test detection
    for name in ["AB15_CompleteModel", "AB12SWArchitecture", "SomeOtherProject"]:
        conv = get_conventions(name)
        print(f"{name} -> {conv['project_type']}: prefix={conv['component_prefix']}, "
              f"swimlanes={conv['swimlanes_in_analysis']}, "
              f"stereotype={conv['analysis_stereotype']}")