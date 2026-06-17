# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Forza Engine Studio Python sidecar.

Freezes the three sidecars (generate / ai / render-json) behind one dispatcher
exe (`fes-engine`) as a ONEDIR bundle. The Tauri bundler ships the resulting
`fes-engine/` folder as a resource, and the Rust backend spawns the exe with a
leading subcommand instead of `python <script>.py`.

Build (from the repo root, via build-release.ps1, or by hand):
    python/.venv/Scripts/python.exe -m PyInstaller python/fes-engine.spec \
        --noconfirm --distpath src-tauri/pyengine --workpath build/pyi

pyopencl is INTENTIONALLY NOT bundled (see python/fd6/shapegen/gpu.py): the
lean-exe design pip-installs it on demand into %LOCALAPPDATA%/FD6/gpu_runtime the
first time the user picks GPU, so the right cp312 wheel/ABI is fetched and the
OpenCL runtime ships with the GPU driver. `pip` IS bundled so that in-process
install works from the frozen exe (it cannot shell out to `python -m pip`,
because sys.executable is the app). With no GPU / no network the engine simply
runs on the bundled CPU path.
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

# SPECPATH is injected by PyInstaller when running a .spec; it is the spec's dir
# (python/). Anchor every path to it so the build is launch-CWD independent.
HERE = SPECPATH  # noqa: F821  (provided by PyInstaller)

datas = []
binaries = []
# The dispatcher imports these lazily by name, so PyInstaller can't see them by
# static analysis — declare them (plus the whole vendored engine package).
hiddenimports = ["sidecar", "image_process", "render_json"]
hiddenimports += collect_submodules("fd6")

# Bundle pip (+ its vendored deps and metadata) so gpu.py's on-demand pyopencl
# install runs in-process inside the frozen app.
pip_datas, pip_binaries, pip_hidden = collect_all("pip")
datas += pip_datas
binaries += pip_binaries
hiddenimports += pip_hidden
datas += copy_metadata("pip")

# numpy / Pillow / requests (+ certifi CA bundle) are handled by PyInstaller's
# built-in hooks; no explicit collection needed.

a = Analysis(
    [os.path.join(HERE, "fes_engine.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy / dev-only deps that creep in via the venv but the app never uses
        # at runtime. pyopencl & friends are installed on demand (see above).
        "pyopencl", "pytools", "siphash24", "cupy", "numba",
        "pytest", "hypothesis", "IPython", "tkinter", "matplotlib",
        "sphinx", "mako", "Cython",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fes-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,            # sidecars talk over stdout/stderr; Rust spawns with
                             # CREATE_NO_WINDOW so no console window ever appears.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="fes-engine",       # -> <distpath>/fes-engine/fes-engine.exe
)
