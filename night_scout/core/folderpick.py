"""Choose a project folder with the system's own dialog (Finder, File Explorer, a Linux picker).

The web UI can't learn a real folder path from the browser, so the server opens the dialog
itself, on the computer it runs on. Only for requests from that computer: the routes check
that (web/app.py `same_machine`). Each dialog runs in its own process, so a stuck or closed
dialog never blocks the server, and only one is open at a time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

PROMPT = "Choose a project folder for Night Scout"
TIMEOUT = 600  # seconds; a dialog left open longer is closed and counts as cancelled

# Windows: a hidden Tk root on top, then the standard folder dialog (Tk 8.6 uses
# IFileDialog, the Explorer-style one). Prints the path, or nothing on cancel.
_TK = """
import sys, tkinter
from tkinter import filedialog
r = tkinter.Tk(); r.withdraw(); r.attributes("-topmost", True)
p = filedialog.askdirectory(parent=r, title=sys.argv[1], initialdir=sys.argv[2], mustexist=True)
r.destroy()
sys.stdout.write(p or "")
"""
# Windows without tkinter (Python installed without Tcl/Tk).
_PS = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description = $env:NS_PROMPT; $d.SelectedPath = $env:NS_START;"
    "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
)


class PickerError(Exception):
    """The dialog couldn't be shown or failed."""


@dataclass(frozen=True)
class Picker:
    kind: str  # "finder", "explorer", "zenity", "kdialog"
    label: str  # what the button hint calls it: "Finder", "File Explorer", "a folder picker"
    cmd: tuple[str, ...]


_lock = threading.Lock()


def _has_tk() -> bool:
    import importlib.util

    return importlib.util.find_spec("tkinter") is not None


def find() -> Picker | None:
    """The dialog this computer can show, or None (then the UI offers a typed path)."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        return Picker("finder", "Finder", ("osascript",))
    if sys.platform == "win32":
        if _has_tk():
            return Picker("explorer", "File Explorer", (sys.executable, "-c", _TK))
        ps = shutil.which("powershell")
        if ps:
            return Picker("explorer", "File Explorer", (ps, "-NoProfile", "-STA", "-Command", _PS))
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        for tool, label in (("zenity", "a folder picker"), ("kdialog", "a folder picker")):
            exe = shutil.which(tool)
            if exe:
                return Picker(tool, label, (exe,))
    return None


def busy() -> bool:
    return _lock.locked()


def _argv(p: Picker, start: str) -> tuple[list[str], dict[str, str] | None]:
    if p.kind == "finder":
        script = (
            f'set f to choose folder with prompt "{PROMPT}" '
            f'default location (POSIX file "{_applescript_escape(start)}")\n'
            "POSIX path of f"
        )
        # `activate` brings the dialog in front of the browser.
        return [*p.cmd, "-e", "activate", "-e", script], None
    if p.kind == "explorer":
        if p.cmd[-1] == _TK:
            return [*p.cmd, PROMPT, start], None
        return list(p.cmd), {**os.environ, "NS_PROMPT": PROMPT, "NS_START": start}
    if p.kind == "zenity":
        return [
            *p.cmd,
            "--file-selection",
            "--directory",
            f"--title={PROMPT}",
            f"--filename={start.rstrip('/')}/",
        ], None
    return [*p.cmd, "--getexistingdirectory", start, "--title", PROMPT], None


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def choose(start: str | None = None, picker: Picker | None = None) -> str | None:
    """Show the dialog and wait. The chosen folder's absolute path, or None if cancelled.

    Raises PickerError if no dialog is available, one is already open, or it fails.
    """
    p = picker or find()
    if p is None:
        raise PickerError("No folder picker on this computer. Type the folder's path instead.")
    if not _lock.acquire(blocking=False):
        raise PickerError("A folder picker is already open. Finish with that one first.")
    try:
        home = str(Path.home())
        begin = start if start and Path(start).is_dir() else home
        argv, env = _argv(p, begin)
        try:
            r = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError as e:
            raise PickerError(f"Couldn't open {p.label}: {e}") from e
        out = r.stdout.strip()
        if r.returncode != 0:
            # osascript: "User canceled. (-128)"; zenity/kdialog: exit 1 with no output.
            # Any other failure (say, macOS refusing the dialog) is reported, not hidden.
            if p.kind == "finder" and "-128" in r.stderr:
                return None
            if p.kind in ("zenity", "kdialog") and r.returncode == 1 and not out:
                return None
            raise PickerError(f"{p.label} failed: {(r.stderr or r.stdout).strip()[:200]}")
        if not out:
            return None
        path = Path(out).expanduser()
        if not path.is_dir():
            raise PickerError(f"{p.label} returned something that isn't a folder: {out[:200]}")
        return str(path.resolve())
    finally:
        _lock.release()
