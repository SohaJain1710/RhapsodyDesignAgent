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
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            _cfg = json.load(f)
    except Exception:
        pass

# ── Public constants ──────────────────────────────────────────────────────────
RUNTIME_DIR = _cfg.get("runtime_dir", r"C:\RhapsodyAIAgent_runtime")
TOOLS_PATH  = _cfg.get("tools_path",  _HERE)
PYTHON_PATH = _cfg.get("python_path", "python")

# Ensure runtime dir exists
os.makedirs(RUNTIME_DIR, exist_ok=True)
