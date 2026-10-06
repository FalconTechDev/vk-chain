#!/usr/bin/env python3
"""Builds vk-chain for Windows: the program, then its installer.

    python build.py                  program and installer
    python build.py --no-installer   just the program, in dist/vk-chain

Needs PyInstaller (`pip install pyinstaller`) and, for the installer, Inno
Setup 6 (https://jrsoftware.org/isinfo.php). The installer step is skipped
with a clear message when Inno Setup is not installed.

The program is checked before it is packaged: it is started once in its
self-test mode, which opens the real window hidden and confirms the page
mounts without an error. A frozen program that starts and shows nothing looks
identical, from outside, to one that works - so the build asks it.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"


# What the program never uses but PyInstaller would otherwise find and bundle.
# pywebview can draw its window with Qt, GTK, Cocoa or Chromium Embedded, and
# imports each lazily, so on a machine that happens to have PyQt5 installed a
# naive build ships hundreds of megabytes of Qt for a window that is drawn by
# the WebView2 runtime Windows already has. bottle (pywebview's local server)
# likewise names template engines it never loads.
_UNUSED = [
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3",
    "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cocoa",
    "webview.platforms.android", "webview.platforms.cef", "webview.platforms.mshtml",
    "jinja2", "mako", "pygments", "IPython", "matplotlib", "numpy", "pandas", "scipy",
    "PIL", "setuptools", "pkg_resources", "pytest",
]
EXCLUDES = [a for m in _UNUSED for a in ("--exclude-module", m)]


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "app.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("не найден __version__ в app.py")
    return m.group(1)


def run(cmd: list, **kw) -> None:
    print("\n$", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def build_program() -> pathlib.Path:
    for d in (DIST / "vk-chain", BUILD):
        shutil.rmtree(d, ignore_errors=True)
    sep = ";" if os.name == "nt" else ":"
    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--noconsole", "--onedir",
        "--name", "vk-chain",
        "--icon", ROOT / "assets" / "icon.ico",
        # The interface and its bundled fonts and React. Not the design's
        # stale demo build, which would only add half a megabyte of mock data.
        "--add-data", f"{ROOT / 'ui' / 'source'}{sep}ui/source",
        "--add-data", f"{ROOT / 'assets' / 'logo.png'}{sep}assets",
        *EXCLUDES,
        "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
        ROOT / "app.py",
    ])
    exe = DIST / "vk-chain" / ("vk-chain.exe" if os.name == "nt" else "vk-chain")
    if not exe.exists():
        raise SystemExit(f"сборка не создала {exe}")
    return exe


def selftest(exe: pathlib.Path) -> None:
    """Starts the built program hidden and reads its own verdict."""
    data = pathlib.Path(tempfile.mkdtemp(prefix="vkchain-build-"))
    env = dict(os.environ, VKCHAIN_DATA=str(data))
    print("\nпроверяю собранную программу…", flush=True)
    try:
        r = subprocess.run([str(exe), "--selftest"], env=env, timeout=120)
    except subprocess.TimeoutExpired:
        raise SystemExit("программа не завершила самопроверку за 120 с")
    verdict = data / "selftest.txt"
    text = verdict.read_text(encoding="utf-8") if verdict.exists() else "(нет файла selftest.txt)"
    print("  ", text)
    if r.returncode != 0 or not text.startswith("PASS"):
        log = data / "app.log"
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace")[-1500:])
        raise SystemExit("самопроверка собранной программы не прошла")
    shutil.rmtree(data, ignore_errors=True)


def find_iscc() -> pathlib.Path | None:
    candidates = [os.environ.get("ISCC")]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidates.append(str(pathlib.Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
            candidates.append(str(pathlib.Path(base) / "Inno Setup 6" / "ISCC.exe"))
    found = shutil.which("iscc")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return pathlib.Path(c)
    return None


def build_installer(ver: str) -> pathlib.Path | None:
    iscc = find_iscc()
    if not iscc:
        print("\nInno Setup 6 не найден: установщик пропущен.")
        print("Поставь его с https://jrsoftware.org/isdl.php и запусти сборку снова.")
        return None
    run([iscc, f"/DAppVersion={ver}", ROOT / "installer" / "vk-chain.iss"])
    out = DIST / f"vk-chain-setup-{ver}.exe"
    return out if out.exists() else None


def main() -> None:
    if os.name != "nt":
        raise SystemExit("установщик собирается под Windows")
    ver = version()
    print(f"vk-chain {ver}")
    exe = build_program()
    selftest(exe)
    if "--no-installer" in sys.argv:
        print(f"\nготово: {exe}")
        return
    out = build_installer(ver)
    if out:
        mb = out.stat().st_size / 1048576
        print(f"\nготово: {out}  ({mb:.1f} МБ)")


if __name__ == "__main__":
    main()
