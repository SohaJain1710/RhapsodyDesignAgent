# Rhapsody Model Assistant

A VS Code chat participant (`@rhapsody`) that generates Detailed Design artifacts in IBM Rhapsody
(BDD, IBD, Activity Diagrams) from requirements, using GitHub Copilot as the LLM.

## Install

1. Download `rhapsody-design-assistant-<version>.vsix`.
2. In VS Code: **Extensions** view → `…` menu → **Install from VSIX…**, or run:
   ```
   code --install-extension rhapsody-design-assistant-0.0.3.vsix
   ```
3. Reload the window. On first start you'll be asked to set up the Python environment. Click
   **Set Up Now**, or run **Rhapsody: Set Up Python Environment** from the Command Palette.
   This creates a virtual environment in `%LOCALAPPDATA%\RhapsodyAIAgent\.venv` and installs the
   required packages. It survives extension upgrades.

### Prerequisites

- Windows with IBM Rhapsody 9.x (project open, COM automation enabled)
- Python 3.10+ (3.11 recommended) on `PATH`, or set `rhapsody.basePythonPath`
- VS Code 1.90+ with GitHub Copilot Chat signed in

## Usage

In the Copilot Chat panel:

```
@rhapsody /design rb_sdm_SafeDataMgt
```

## Settings

| Setting | Purpose |
|---|---|
| `rhapsody.pythonPath` | Use an existing Python interpreter instead of the managed venv |
| `rhapsody.basePythonPath` | Python used to create the managed venv (auto-detected if empty) |
| `rhapsody.runtimeDir` | Runtime output folder (default `C:\RhapsodyAIAgent_runtime`) |
| `rhapsody.toolsRoot` | Use a `tools/` folder from a repo checkout instead of the bundled one |

## Building the VSIX

```
npm install
npm run package
```

This produces `rhapsody-design-assistant-<version>.vsix` in the repo root.
