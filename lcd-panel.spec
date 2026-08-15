# -*- mode: python ; coding: utf-8 -*-
#
# Build:  venv\Scripts\pyinstaller.exe lcd-panel.spec
# Output: dist/LcdPanel/  (zip this folder to distribute)
#
# onedir rather than onefile deliberately: onefile re-extracts to a temp dir on
# every launch, which is wasteful for an always-running background app and
# moves sys._MEIPASS somewhere the LibreHardwareMonitor DLL lookup can't follow.

import os
from PyInstaller.utils.hooks import collect_data_files

# pythonnet's runtime/ folder holds ~97 .NET assemblies that the CLR resolves
# at load time. PyInstaller's static analysis only finds Python.Runtime.dll, so
# the rest have to be collected explicitly - without them, loading
# LibreHardwareMonitor dies with an access violation (0xC0000005) instead of a
# Python traceback, and only on the elevated path where LHM is imported at all.
pythonnet_data = collect_data_files('pythonnet', include_py_files=False)

a = Analysis(
    ['status_panel.py'],
    pathex=[],
    binaries=[],
    datas=pythonnet_data + [
        # Whole LHM folder, not just the two DLLs we reference by name:
        # clr.AddReference resolves the System.* assemblies at runtime, so
        # cherry-picking produces a failure that only shows up on someone
        # else's machine.
        ('external/LibreHardwareMonitor', 'external/LibreHardwareMonitor'),
        # Bundled but never auto-run - the app only prints the command.
        ('external/PawnIO', 'external/PawnIO'),
        # Shipped defaults; the user's editable copy sits next to the exe.
        ('config.dist.ini', '.'),
        ('LICENSE', '.'),
    ],
    hiddenimports=[
        # COM plumbing for the microphone mute/name query - pycaw resolves
        # these dynamically, so PyInstaller's static analysis misses them.
        'comtypes',
        'comtypes.stream',
        'pycaw',
        'pycaw.pycaw',
        # pythonnet's CLR bridge, used to load LibreHardwareMonitorLib.
        'clr',
        'clr_loader',
        # Fonts/imaging path used by the renderer.
        'PIL._imaging',
    ],
    # pythonnet ships its own PyInstaller hook (hook-clr.py); point at it so
    # the CLR bootstrap is set up correctly rather than half-collected.
    hookspath=[os.path.join('venv', 'Lib', 'site-packages', 'pythonnet', '_pyinstaller')],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter drives the Settings window (status_panel.py's
    # _build_settings_window) - it used to be excluded here to save ~10MB
    # when the app had no GUI beyond the panel itself.
    excludes=['matplotlib', 'numpy.testing', 'pytest'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LcdPanel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=True so --install-autostart and the PawnIO hint are readable
    # when run from a terminal. The normal run path hides its own console
    # window at startup (status_panel.py: hide_console_window()) and lives
    # in the tray instead - console=True only affects the CLI-flag paths.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='.',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LcdPanel',
)
