"""
SCAD2STEP FreeCAD Macro — Import OpenSCAD models directly in FreeCAD

This macro adds a "SCAD2STEP" option to FreeCAD's Import menu to convert .scad/.csg files to STEP.
Place this file in FreeCAD's macro folder (~/.FreeCAD/Macro or %APPDATA%\FreeCAD\Macro on Windows).

Usage:
  1. In FreeCAD, go to Macro → Execute macro
  2. Select this file
  3. Or use Macro → Recent macros → SCAD2STEP after first run
"""

import os
import sys
import subprocess
import tempfile
import FreeCAD
import Part


def find_scad2step():
    """Find SCAD2STEP installation directory."""
    # Common installation locations
    locations = [
        os.path.expanduser("~/SCAD2STEP"),
        os.path.expanduser("~/Documents/SCAD2STEP"),
        os.path.expanduser("~/Downloads/SCAD2STEP"),
        "/opt/SCAD2STEP",
        "C:\\Program Files\\SCAD2STEP",
        "C:\\Users\\{}\\SCAD2STEP".format(os.environ.get("USERNAME", "")),
    ]
    for loc in locations:
        if os.path.isdir(loc):
            launcher = None
            if sys.platform.startswith("win"):
                launcher = os.path.join(loc, "SCAD2STEP (Windows).bat")
            elif sys.platform == "darwin":
                launcher = os.path.join(loc, "SCAD2STEP (Mac).command")
            else:
                launcher = os.path.join(loc, "scad2step-linux.sh")
            if os.path.exists(launcher):
                return loc
    return None


def convert_scad(scad_path):
    """Convert a .scad/.csg file to STEP using SCAD2STEP."""
    scad2step_dir = find_scad2step()
    if not scad2step_dir:
        FreeCAD.Console.PrintError("SCAD2STEP not found. Please install SCAD2STEP or set its location.\n")
        return None

    stem = os.path.splitext(scad_path)[0]
    step_path = stem + ".step"

    # Run conversion
    if sys.platform.startswith("win"):
        launcher = os.path.join(scad2step_dir, "SCAD2STEP (Windows).bat")
        cmd = ["cmd", "/c", launcher, scad_path, "-o", step_path]
    elif sys.platform == "darwin":
        launcher = os.path.join(scad2step_dir, "SCAD2STEP (Mac).command")
        cmd = ["bash", launcher, scad_path, "-o", step_path]
    else:
        launcher = os.path.join(scad2step_dir, "scad2step-linux.sh")
        cmd = ["bash", launcher, scad_path, "-o", step_path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            FreeCAD.Console.PrintError(f"Conversion failed: {result.stderr}\n")
            return None
        if not os.path.exists(step_path):
            FreeCAD.Console.PrintError(f"Output file not created: {step_path}\n")
            return None
        return step_path
    except Exception as e:
        FreeCAD.Console.PrintError(f"Error running SCAD2STEP: {e}\n")
        return None


def import_scad():
    """Main function: ask for a .scad file and import it."""
    doc = FreeCAD.activeDocument()
    if not doc:
        doc = FreeCAD.newDocument()

    # File dialog
    from PySide2 import QtWidgets
    file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
        None,
        "Import OpenSCAD Model",
        "",
        "OpenSCAD (*.scad *.csg);;All Files (*)"
    )
    if not file_path:
        return

    FreeCAD.Console.PrintLog(f"Converting {file_path}...\n")
    step_path = convert_scad(file_path)
    if not step_path:
        return

    FreeCAD.Console.PrintLog(f"Importing {step_path}...\n")
    try:
        shape = Part.read(step_path)
        obj = doc.addObject("Part::Feature", os.path.splitext(os.path.basename(file_path))[0])
        obj.Shape = shape
        FreeCAD.Console.PrintLog(f"Imported successfully as '{obj.Label}'.\n")
        FreeCAD.Gui.Selection.addSelection(obj)
        doc.recompute()
    except Exception as e:
        FreeCAD.Console.PrintError(f"Failed to import STEP: {e}\n")


if __name__ == "__main__":
    try:
        import_scad()
    except Exception as e:
        FreeCAD.Console.PrintError(f"Macro failed: {e}\n")
