"""
Install SCAD2STEP plugins for FreeCAD and Fusion 360.

Usage:
    python install_plugins.py freecad       # Install FreeCAD macro
    python install_plugins.py fusion360     # Install Fusion 360 add-in
    python install_plugins.py all           # Install all plugins
"""
import os
import sys
import shutil
import json


HERE = os.path.dirname(os.path.abspath(__file__))
MACRO_FILE = os.path.join(HERE, "freecad_macro.py")
ADDIN_FILE = os.path.join(HERE, "fusion360_addin.py")


def get_freecad_macro_dir():
    """Get FreeCAD macro directory."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/FreeCAD/Macro")
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(appdata, "FreeCAD", "Macro")
    else:
        return os.path.expanduser("~/.FreeCAD/Macro")


def get_fusion360_addin_dir():
    """Get Fusion 360 add-in directory."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Autodesk/Fusion 360/API/Python/Addins/SCAD2STEP")
    elif sys.platform.startswith("win"):
        return os.path.expanduser("~/Autodesk/webdeploy/Fusion 360 API/Python/Addins/SCAD2STEP")
    else:
        return os.path.expanduser("~/.local/share/Autodesk/Fusion 360/API/Python/Addins/SCAD2STEP")


def install_freecad():
    """Install FreeCAD macro."""
    if not os.path.exists(MACRO_FILE):
        print(f"ERROR: {MACRO_FILE} not found")
        return False

    macro_dir = get_freecad_macro_dir()
    os.makedirs(macro_dir, exist_ok=True)

    dest = os.path.join(macro_dir, "SCAD2STEP.FCMacro")
    try:
        shutil.copy2(MACRO_FILE, dest)
        print(f"✓ FreeCAD macro installed to {dest}")
        print(f"  Usage: In FreeCAD, go to Macro → Execute macro → Select SCAD2STEP.FCMacro")
        return True
    except Exception as e:
        print(f"✗ Failed to install FreeCAD macro: {e}")
        return False


def install_fusion360():
    """Install Fusion 360 add-in."""
    if not os.path.exists(ADDIN_FILE):
        print(f"ERROR: {ADDIN_FILE} not found")
        return False

    addin_dir = get_fusion360_addin_dir()
    os.makedirs(addin_dir, exist_ok=True)

    # Copy add-in script
    addin_script = os.path.join(addin_dir, "SCAD2STEP.py")
    try:
        shutil.copy2(ADDIN_FILE, addin_script)
        print(f"✓ Fusion 360 add-in script installed to {addin_script}")
    except Exception as e:
        print(f"✗ Failed to install Fusion 360 add-in: {e}")
        return False

    # Create manifest.xml for Fusion 360
    manifest = os.path.join(addin_dir, "manifest.xml")
    manifest_content = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest xmlns="http://autodesk.com/adsk/manifest/2011">
    <Id>SCAD2STEP</Id>
    <Name>SCAD2STEP</Name>
    <Description>Import OpenSCAD models to Fusion 360 using SCAD2STEP</Description>
    <Author>SCAD2STEP</Author>
    <SupportUrl>https://github.com/mattar/scad2step</SupportUrl>
    <Version>1.0.0</Version>
    <RunOnStartup>false</RunOnStartup>
    <UiType>Panel</UiType>
    <Commands>
        <Command Name="ScadToStepCommand">
            <DisplayName>Import SCAD with SCAD2STEP</DisplayName>
        </Command>
    </Commands>
</manifest>'''
    try:
        with open(manifest, "w") as f:
            f.write(manifest_content)
        print(f"✓ Fusion 360 manifest created at {manifest}")
        print(f"  Usage: In Fusion 360, go to File → Import → Use the SCAD2STEP command")
        return True
    except Exception as e:
        print(f"✗ Failed to create Fusion 360 manifest: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    target = sys.argv[1].lower()
    success = True

    if target in ("all", "freecad"):
        print("Installing FreeCAD macro...")
        if not install_freecad():
            success = False
        print()

    if target in ("all", "fusion360"):
        print("Installing Fusion 360 add-in...")
        if not install_fusion360():
            success = False
        print()

    if target not in ("all", "freecad", "fusion360"):
        print(f"Unknown target: {target}")
        print(__doc__)
        return 1

    if success:
        print("✓ Plugin installation complete!")
        return 0
    else:
        print("✗ Some plugins failed to install.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
