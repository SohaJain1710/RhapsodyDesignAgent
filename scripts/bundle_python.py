#!/usr/bin/env python3
"""
Build a self-contained Windows x64 Python runtime for the VSIX.

  python scripts/bundle_python.py            # -> ./python/python.exe + all packages
  python scripts/bundle_python.py --requirements requirements.txt   # latest versions

Downloads the official CPython build for Windows (python.org's NuGet package),
then installs requirements.txt into it as Windows wheels. Runs on Linux,
macOS or Windows: pip only downloads and unpacks win_amd64 wheels, it never
runs the bundled interpreter. The result needs no system Python, pip, venv or
network access on the user's machine.
"""
import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NUGET_URL = "https://api.nuget.org/v3-flatcontainer/python/{v}/python.{v}.nupkg"

# Parts of the standard library the tools never use (saves ~40 MB).
PRUNE = [
    "Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/lib2to3",
    "Lib/ensurepip", "Lib/venv", "Lib/pydoc_data", "Lib/site-packages/pip",
    "tcl", "include", "libs", "Tools", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-version", default="3.11.9")
    ap.add_argument("--out", default=os.path.join(ROOT, "python"))
    # requirements-lock.txt pins exact versions so every VSIX build is identical.
    lock = os.path.join(ROOT, "requirements-lock.txt")
    ap.add_argument("--requirements",
                    default=lock if os.path.exists(lock) else os.path.join(ROOT, "requirements.txt"))
    args = ap.parse_args()

    v = args.python_version
    major_minor = "".join(v.split(".")[:2])          # "311"
    out = os.path.abspath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)

    url = NUGET_URL.format(v=v)
    print(f"[bundle] downloading {url}")
    data = urllib.request.urlopen(url).read()
    print(f"[bundle] {len(data)/1e6:.1f} MB, sha256 {hashlib.sha256(data).hexdigest()}")

    # The nupkg is a zip; the interpreter lives under tools/
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if not name.startswith("tools/") or name.endswith("/"):
                continue
            dest = os.path.join(out, name[len("tools/"):])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(name) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if not os.path.exists(os.path.join(out, "python.exe")):
        sys.exit("[bundle] ERROR: python.exe not found in NuGet package")

    for rel in PRUNE:
        p = os.path.join(out, rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)

    site = os.path.join(out, "Lib", "site-packages")
    os.makedirs(site, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install",
           "--target", site,
           "--platform", "win_amd64",
           "--python-version", v.rsplit(".", 1)[0],
           "--implementation", "cp",
           "--abi", f"cp{major_minor}",
           "--only-binary=:all:",
           "--no-compile", "--no-warn-script-location", "--disable-pip-version-check",
           "-r", args.requirements]
    print("[bundle] " + " ".join(cmd))
    subprocess.check_call(cmd)

    # Console-script launchers pip generated for the *build* OS are useless here.
    shutil.rmtree(os.path.join(site, "bin"), ignore_errors=True)
    for root, dirs, _ in os.walk(out):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d))
                dirs.remove(d)

    frozen = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--path", site, "--format=freeze",
         "--disable-pip-version-check"],
        capture_output=True, text=True).stdout
    with open(os.path.join(out, "BUNDLE_INFO.txt"), "w", encoding="utf-8") as f:
        f.write(f"CPython {v} (win_amd64, from {url})\n\n{frozen}")

    size = sum(os.path.getsize(os.path.join(r, n)) for r, _, fs in os.walk(out) for n in fs)
    print(f"[bundle] done: {out} ({size/1e6:.0f} MB)")


if __name__ == "__main__":
    main()
