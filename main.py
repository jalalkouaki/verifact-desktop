"""
main.py - Veri-Fact Desktop launcher
====================================

Double-click ``run_app.bat`` (Windows) or run ``python main.py`` and a native
desktop window opens immediately. There is deliberately NO web server, NO
localhost URL, NO browser and NO extension anywhere in this project.

WHAT THIS FILE IS RESPONSIBLE FOR
---------------------------------
1. Making sure Python is new enough (3.10+).
2. Making sure the ``verifact`` package next to this file is importable, even
   when the app is launched from a different working directory (which is what
   happens when you double-click the .bat from Explorer).
3. Checking that tkinter itself is present - on some Linux distributions it is
   a separate system package - and printing a helpful message if not.
4. Constructing :class:`verifact.gui.VeriFactApp` and entering the Tk main loop.
5. Catching any catastrophic startup error and showing it in a message box
   rather than a console window that vanishes instantly.

Everything else lives in the ``verifact`` package - see ``verifact/__init__.py``
for the map of that folder.
"""

from __future__ import annotations

import os
import sys
import traceback

# ---------------------------------------------------------------------------
# 1. Python version gate.
# ---------------------------------------------------------------------------
MINIMUM_PYTHON = (3, 10)

if sys.version_info < MINIMUM_PYTHON:
    sys.stderr.write(
        f"Veri-Fact Desktop needs Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} "
        f"or newer.\nYou are running {sys.version.split()[0]} from {sys.executable}\n"
    )
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# 2. Make the folder containing this file importable no matter where the
#    process was started from. Double-clicking a .bat in Explorer sets the
#    working directory to the .bat's folder, but a shortcut may not.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _fatal(message: str) -> None:
    """Report a startup failure through a GUI popup when possible.

    A double-clicked .bat closes its console the instant Python exits, so a
    plain ``print`` would be invisible. We try a Tk message box first and only
    fall back to stderr if even Tk is unavailable.
    """
    sys.stderr.write(message + "\n")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Veri-Fact Desktop - startup failed", message)
        root.destroy()
    except Exception:
        pass


def main() -> int:
    # -- 3. tkinter availability check ------------------------------------
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        sys.stderr.write(
            "Veri-Fact Desktop needs tkinter, which ships with the official\n"
            "python.org installers.\n"
            "  Windows : re-run the installer and tick 'tcl/tk and IDLE'.\n"
            "  Debian  : sudo apt install python3-tk\n"
            "  Fedora  : sudo dnf install python3-tkinter\n"
            f"\nImport error: {exc}\n"
        )
        return 1

    # -- 4. build and run the application ---------------------------------
    try:
        from verifact.gui import VeriFactApp
        app = VeriFactApp(base_dir=BASE_DIR)
        app.run()
        return 0
    except Exception:
        _fatal(
            "Veri-Fact Desktop could not start.\n\n"
            + traceback.format_exc()
            + "\nMost common causes:\n"
              "  - dependencies missing: pip install -r requirements.txt\n"
              "  - the 'verifact' folder is not next to main.py\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
