# RhapsodyAIAgent — Installation Guide

## Prerequisites

### 1. Node.js
- **Required**: Node.js 18+ (for express 5) or Node.js 14+ (for express 4)
- **Current setup uses**: express 4 with Node 14.16.1
- **Recommended**: Install Node.js 18 LTS from https://nodejs.org/
- Verify: `node --version`

### 2. Python
- **Required**: Python 3.11+
- **Recommended**: Python 3.11.x (tested)
- Download from https://www.python.org/downloads/
- During install: ✅ Check "Add Python to PATH"
- Verify: `python --version`

### 3. IBM Rhapsody
- Rhapsody must be open with the AB12 project loaded
- COM automation must be enabled (default in Rhapsody)

### 4. VS Code
- Version 1.85+
- GitHub Copilot extension installed and signed in
- Chat extension enabled

---

## Directory Structure

```
C:\Users\jio2kor\OneDrive - Bosch Group\RhapsodyAIAgent\
├── .venv\                    ← Python virtual environment
├── tools\                    ← All Python tools
│   ├── llm_node.py
│   ├── design_graph_unified.py
│   ├── read_all_requirements.py
│   ├── read_usecase.py
│   ├── read_bdd.py
│   ├── bdd_to_mermaid.py
│   ├── mermaid_to_bdd.py
│   ├── create_bdd.py
│   ├── read_ibd.py
│   ├── ibd_to_mermaid.py
│   ├── mermaid_to_ibd.py
│   ├── create_ibd.py
│   ├── read_detailed_ad.py
│   ├── ad_to_mermaid.py
│   ├── mermaid_to_ad.py
│   ├── create_activity_diagram.py
│   ├── create_operation_ad.py
│   ├── rhapsody_com.py
│   ├── project_config.py
│   └── confirm_changes.py
├── extension.js              ← VS Code extension entry point
├── package.json              ← Extension manifest
└── node_modules\             ← JS dependencies

C:\RhapsodyAIAgent_runtime\   ← Runtime outputs (outside OneDrive)
├── design_checkpoints.db     ← LangGraph checkpoint DB
├── *.json                    ← Intermediate outputs
└── *.mmd                     ← Mermaid files
```

---

## Step 1: Python Virtual Environment

```powershell
cd "C:\Users\jio2kor\OneDrive - Bosch Group\RhapsodyAIAgent"

# Create virtual environment
python -m venv .venv

# Activate
.venv\Scripts\activate

# Verify
python --version   # Should be 3.11+
```

---

## Step 2: Python Packages

```powershell
# Activate venv first
.venv\Scripts\activate

# Core dependencies
pip install pywin32              # Rhapsody COM automation
pip install langgraph            # LangGraph pipeline framework
pip install langchain            # LangChain base
pip install langchain-openai     # OpenAI-compatible LLM client
pip install openpyxl             # Excel reading
pip install aiosqlite            # Async SQLite for LangGraph checkpointer

# Install pywin32 post-install script (required for COM)
python .venv\Scripts\pywin32_postinstall.py -install
```

### Full requirements.txt

```
pywin32>=306
langgraph>=0.2.0
langchain>=0.3.0
langchain-openai>=0.2.0
openpyxl>=3.1.0
aiosqlite>=0.19.0
```

Save as `requirements.txt` then:
```powershell
pip install -r requirements.txt
python .venv\Scripts\pywin32_postinstall.py -install
```

---

## Step 3: VS Code Extension Setup

```powershell
cd "C:\Users\jio2kor\.vscode\extensions\oss.rhapsody-dd-Assist-0.0.2"

# Install JS dependencies
npm install express@4      # HTTP proxy server (Node 14 compatible)
```

### package.json — Required entries

Ensure these exist in your `package.json`:

```json
{
  "engines": {
    "vscode": "^1.85.0"
  },
  "activationEvents": ["onStartupFinished"],
  "contributes": {
    "chatParticipants": [
      {
        "id": "rhapsody.ddgen",
        "name": "rhapsody",
        "description": "Generate Detailed Design from requirements",
        "isSticky": true,
        "commands": [
          {"name": "design",        "description": "Generate design from requirements"},
          {"name": "design_resume", "description": "Resume paused design run"},
          {"name": "scan",          "description": "Scan component interfaces"},
          {"name": "scan_ibd",      "description": "Read IBD ports and interfaces"}
        ]
      }
    ],
    "commands": [
      {
        "command": "rhapsody.design.source",
        "title": "Rhapsody: Select Requirement Source"
      }
    ]
  }
}
```

---

## Step 4: Runtime Directory

```powershell
# Create runtime directory (outside OneDrive to avoid sync conflicts)
mkdir C:\RhapsodyAIAgent_runtime
```

---

## Step 5: Reload Extension

After all setup:
1. Open VS Code
2. `Ctrl+Shift+P` → `Developer: Reload Window`
3. Verify extension is running:
   `Ctrl+Shift+P` → `Developer: Show Running Extensions` → look for `rhapsody`

---

## Step 6: Verify Setup

```powershell
# Activate venv
cd "C:\Users\jio2kor\OneDrive - Bosch Group\RhapsodyAIAgent"
.venv\Scripts\activate

# Test COM connection (Rhapsody must be open)
python tools\rhapsody_com.py

# Test LLM proxy (VS Code must be open with Copilot)
python tools\llm_node.py "Say hello"

# Test requirements reader (Rhapsody must be open with project)
python tools\read_all_requirements.py --component rb_sdm_SafeDataMgt
```

---

## Usage

Once installed, in VS Code chat:

```
@rhapsody /design rb_sdm_SafeDataMgt
```

The agent will ask:
```
📋 Requirements Source for rb_sdm_SafeDataMgt
[🗄 From Rhapsody SRS]  [📊 From Excel File]
```

Click a button to start the design pipeline.

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `Cannot find module 'express'` | `npm install express@4` in extension dir |
| `No activated agent with id rhapsody.ddgen` | Reload VS Code window |
| `pywintypes.com_error` | Open Rhapsody with project before running |
| `LLM proxy unavailable` | Open VS Code chat first to initialize proxy |
| `No module named 'win32com'` | `pip install pywin32` + run postinstall script |
| `langgraph not found` | `pip install langgraph` in venv |
| `SqliteSaver error` | `pip install aiosqlite` |

---

## Version Summary

| Component | Required | Tested |
|-----------|----------|--------|
| Python | 3.11+ | 3.11.x |
| Node.js | 14+ (18+ recommended) | 14.16.1 |
| express | 4.x (Node 14) / 5.x (Node 18+) | 4.22.2 |
| pywin32 | 306+ | latest |
| langgraph | 0.2.0+ | latest |
| VS Code | 1.85+ | latest |
| Rhapsody | 9.0+ | 9.x |
