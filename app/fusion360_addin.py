"""
SCAD2STEP Fusion 360 Add-in — Import OpenSCAD models in Fusion 360

This add-in adds a "SCAD2STEP" command to Fusion 360's Import menu.

Installation:
  1. Copy this file to ~/Autodesk/webdeploy/Fusion 360 API/Python/Addins/SCAD2STEP/
  2. Or use the Fusion 360 Add-in Manager to install it
"""

import os
import sys
import subprocess
import traceback
import adsk.core
import adsk.fusion
import adsk.cam


# Global variables
handlers_ = []
ADDIN_NAME = "SCAD2STEP"


def find_scad2step():
    """Find SCAD2STEP installation directory."""
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


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            eventArgs = adsk.core.CommandEventArgs.cast(args)
            app = adsk.core.Application.get()
            ui = app.userInterface

            # File dialog
            dialogs = ui.fileBrowser
            filters = "OpenSCAD (*.scad;*.csg)|*.scad;*.csg|All Files (*.*)|*.*"
            dialog_result = dialogs.browsForOpenFile("Select OpenSCAD Model", "", filters)

            if dialog_result[0] != adsk.core.BrowserItemEventArgs.OpenButtonClicked:
                return

            scad_path = dialog_result[1][0]
            ui.messageBox(f"Converting {scad_path}...")

            # Find SCAD2STEP
            scad2step_dir = find_scad2step()
            if not scad2step_dir:
                ui.messageBox("SCAD2STEP not found. Please install SCAD2STEP.", ADDIN_NAME)
                return

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

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                ui.messageBox(f"Conversion failed: {result.stderr}", ADDIN_NAME)
                return

            if not os.path.exists(step_path):
                ui.messageBox(f"Output file not created: {step_path}", ADDIN_NAME)
                return

            # Import to Fusion
            try:
                doc = adsk.fusion.Design.cast(app.activeDocument.design)
                if not doc:
                    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
                    doc = doc.design

                # Use importManager to import the STEP file
                importManager = doc.importManager
                importOptions = importManager.createImportOptions(step_path)
                importManager.importToTarget(importOptions, doc.rootComponent)

                ui.messageBox(f"Imported successfully: {os.path.basename(step_path)}", ADDIN_NAME)
            except Exception as e:
                ui.messageBox(f"Failed to import STEP: {str(e)}", ADDIN_NAME)

        except Exception as e:
            app = adsk.core.Application.get()
            ui = app.userInterface
            ui.messageBox(f"Command failed: {str(e)}\n\n{traceback.format_exc()}", ADDIN_NAME)


def run(context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Create command
        cmdDef = ui.commandDefinitions.addButtonDefinition(
            "ScadToStepCommand",
            "Import SCAD with SCAD2STEP",
            "Convert an OpenSCAD model to STEP and import it",
            "."
        )

        # Add handler
        onCommandExecuted = CommandExecuteHandler()
        cmdDef.commandCreated.add(onCommandExecuted)
        handlers_.append(onCommandExecuted)

        # Add to File menu
        importPanel = ui.toolbars.itemById("FileImport")
        if importPanel:
            importPanel.controls.addCommand(cmdDef, "FileImportStep")

        ui.messageBox("SCAD2STEP add-in loaded. Use File → Import → Import SCAD with SCAD2STEP", ADDIN_NAME)

    except Exception as e:
        if ui:
            ui.messageBox(f"Add-in failed to load: {str(e)}\n\n{traceback.format_exc()}", ADDIN_NAME)


def stop(context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmdDef = ui.commandDefinitions.itemById("ScadToStepCommand")
        if cmdDef:
            cmdDef.deleteMe()
    except Exception:
        pass
