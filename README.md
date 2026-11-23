# Rhapsody Model Assistant

A VS Code chat participant (`@rhapsody`) that generates Detailed Design artifacts in IBM Rhapsody
(BDD, IBD, Activity Diagrams) from requirements, using GitHub Copilot as the LLM.

## Install

1. Download `rhapsody-design-assistant-win32-x64-<version>.vsix` from the repo's **Releases** page
   (or, for an untagged build, from **Actions → Build VSIX → a run → Artifacts**).
2. In VS Code: **Extensions** view → `…` menu → **Install from VSIX…**, or run:
   ```
   code --install-extension rhapsody-design-assistant-win32-x64-0.0.3.vsix
   ```
3. Reload the window. That's it: the VSIX contains its own Python 3.11 with every package
   pre-installed (LangGraph, LangChain, pywin32, openpyxl, …), so no Python install, pip,
   virtual environment or internet access is needed.

### Prerequisites

- Windows with IBM Rhapsody 9.x (project open, COM automation enabled)
- VS Code 1.90+ with GitHub Copilot Chat signed in

## Usage

In the Copilot Chat panel:

```
@rhapsody /design rb_sdm_SafeDataMgt
```

## Settings

| Setting | Purpose |
|---|---|
| `rhapsody.pythonPath` | Use your own Python interpreter instead of the bundled one |
| `rhapsody.basePythonPath` | Python used to create the managed venv (auto-detected if empty) |
| `rhapsody.runtimeDir` | Runtime output folder (default `C:\RhapsodyAIAgent_runtime`) |
| `rhapsody.toolsRoot` | Use a `tools/` folder from a repo checkout instead of the bundled one |

## Building the VSIX

GitHub Actions (`.github/workflows/build-vsix.yml`) builds the VSIX on every push. To publish a release:

```
git tag v0.0.4
git push origin v0.0.4
```

The tag sets the extension version, and the VSIX is attached to a new GitHub Release.

To build locally (Node.js 18+ and Python 3.9+; works on Windows, Linux or macOS):

```
npm install
python scripts/bundle_python.py
npm run package
```

`bundle_python.py` downloads the official CPython 3.11.9 for Windows and installs the exact
versions in `requirements-lock.txt` into `python/`. To upgrade packages, edit
`requirements.txt`, run `python scripts/bundle_python.py --requirements requirements.txt`,
and copy the package list from `python/BUNDLE_INFO.txt` into `requirements-lock.txt`.

This produces `rhapsody-design-assistant-win32-x64-<version>.vsix` in the repo root.
