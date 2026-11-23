"""
rhapsody_connect.py
-------------------
Single place to connect to the RUNNING Rhapsody instance.

Handles two common Windows problems:
  1. A broken pywin32 COM cache (win32com gen_py), e.g.
       "module 'win32com.gen_py.<GUID>' has no attribute 'CLSIDToClassMap'"
     The cache is only generated code, so broken entries are deleted and
     regenerated on demand.
  2. Several Rhapsody processes running (or a leftover hidden rhapsody.exe):
     GetActiveObject() may return an instance WITHOUT an open project. All
     running instances are checked and the one with an open project is used.

win32com is imported lazily so this module can be imported anywhere.
"""
import os
import sys

PROGID = "Rhapsody2.Application"
_gen_py_checked = False


def _log(msg: str) -> None:
    print(f"[Rhapsody] {msg}", file=sys.stderr)


def repair_com_cache() -> None:
    """Delete broken win32com gen_py modules (only once per process)."""
    global _gen_py_checked
    if _gen_py_checked:
        return
    _gen_py_checked = True
    try:
        import importlib
        import shutil
        from win32com.client import gencache
        gen_dir = gencache.GetGeneratePath()
    except Exception:
        return

    broken = []
    for name in os.listdir(gen_dir):
        path = os.path.join(gen_dir, name)
        if name.startswith("__") or name.endswith(".dat"):
            continue
        if os.path.isdir(path):
            mod_name = name
        elif name.endswith(".py"):
            mod_name = name[:-3]
        else:
            continue
        try:
            mod = importlib.import_module("win32com.gen_py." + mod_name)
            getattr(mod, "CLSIDToClassMap")
        except Exception:
            broken.append(path)

    for path in broken:
        _log(f"Removing broken COM cache entry: {path}")
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except Exception as e:
            _log(f"  could not remove ({e}) - delete the folder {gen_dir} manually")

    if broken:
        try:
            gencache.Rebuild(verbose=0)
        except Exception:
            pass


def _active_project(app):
    try:
        return app.activeProject()
    except Exception:
        return None


def _running_instances():
    """Yield every running Rhapsody instance registered in the Running Object Table."""
    import pythoncom
    import pywintypes
    import win32com.client
    try:
        clsid = str(pywintypes.IID(PROGID)).upper().strip("{}")
        rot = pythoncom.GetRunningObjectTable()
        ctx = pythoncom.CreateBindCtx(0)
        monikers = list(rot.EnumRunning())
    except Exception:
        return
    for mk in monikers:
        try:
            if clsid not in mk.GetDisplayName(ctx, None).upper():
                continue
            obj = rot.GetObject(mk)
            yield win32com.client.Dispatch(obj.QueryInterface(pythoncom.IID_IDispatch))
        except Exception:
            continue


def get_active_rhapsody():
    """Return the running Rhapsody application that has a project open."""
    import win32com.client
    repair_com_cache()

    try:
        app = win32com.client.GetActiveObject(PROGID)
    except Exception as e:
        raise RuntimeError(
            "Could not connect to Rhapsody. Make sure Rhapsody is running, and that "
            "Rhapsody and VS Code are started the same way (both normally, or both "
            f"'as administrator'). Details: {e}"
        ) from e

    if _active_project(app) is not None:
        return app

    for inst in _running_instances():
        if _active_project(inst) is not None:
            _log("Connected to another running Rhapsody instance that has a project open")
            return inst

    raise RuntimeError(
        "Rhapsody is running but no project is open in it. Open your project in "
        "Rhapsody. If it already is open, more than one rhapsody.exe is probably "
        "running: close the extra ones (check Task Manager > Details for leftover "
        "rhapsody.exe processes) and try again."
    )


def get_active_rhapsody_and_project():
    app = get_active_rhapsody()
    return app, app.activeProject()
