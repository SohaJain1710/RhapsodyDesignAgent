"""
config.py
---------
Reads runtime configuration from config.json (written by setup.ps1).
Falls back to defaults if not found.
"""
import os
import json

# Find config.json — it's in the repo root (parent of tools/)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CONFIG_FILE = os.path.join(_ROOT, "config.json")

_cfg = {}
if os.path.exists(_CONFIG_FILE):
    try:
        with open(_CONFIG_FILE, encoding="utf-8-sig") as f:   # tolerate a BOM
            _cfg = json.load(f)
    except Exception:
        pass

# ── Public constants ──────────────────────────────────────────────────────────
# RHAPSODY_RUNTIME_DIR is set by the VS Code extension (VSIX installs).
RUNTIME_DIR = (os.environ.get("RHAPSODY_RUNTIME_DIR")
               or _cfg.get("runtime_dir")
               or (r"C:\RhapsodyAIAgent_runtime" if os.name == "nt"
                   else os.path.join(os.path.expanduser("~"), "RhapsodyAIAgent_runtime")))
TOOLS_PATH  = _cfg.get("tools_path",  _HERE)
PYTHON_PATH = _cfg.get("python_path", "python")

# Ensure runtime dir exists
os.makedirs(RUNTIME_DIR, exist_ok=True)
