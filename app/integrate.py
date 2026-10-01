"""
Add SCAD2STEP to your file manager's right-click menu (the closest thing to an OpenSCAD
plugin — OpenSCAD itself has no plugin system).

  Linux (Nemo — Linux Mint's file manager, Nautilus/Files, plus the app menu):
      right-click a .scad → "Convert to STEP" / "Live-link to STEP (updates on save)"
  Windows: right-click a .scad → Send to → "STEP (SCAD2STEP)" / "STEP live link (SCAD2STEP)"

  scad2step integrate            install
  scad2step integrate --remove   remove again
"""
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TAG = "SCAD2STEP"


def _write(path, text, exe=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\n" if not sys.platform.startswith("win") else None) as f:
        f.write(text)
    if exe:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("  added", path)


def _rm(path):
    if os.path.exists(path):
        os.remove(path)
        print("  removed", path)


def linux(remove):
    home = os.path.expanduser("~")
    share = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    wrapper = os.path.join(share, "scad2step", "scad2step")
    nemo = os.path.join(share, "nemo", "actions")
    naut = os.path.join(share, "nautilus", "scripts")
    apps = os.path.join(share, "applications")
    files = [wrapper, os.path.join(nemo, "scad2step-convert.nemo_action"),
             os.path.join(nemo, "scad2step-live.nemo_action"),
             os.path.join(naut, "Convert to STEP"), os.path.join(naut, "Live-link to STEP"),
             os.path.join(apps, "scad2step.desktop")]
    if remove:
        for f in files:
            _rm(f)
        return 0
    launcher = os.path.join(ROOT, "scad2step-linux.sh")
    _write(wrapper, f'#!/bin/bash\n# {TAG}: runs the portable app wherever its folder is\n'
                    f'exec bash "{launcher}" "$@"\n', exe=True)
    _write(files[1], "[Nemo Action]\nName=Convert to STEP\n"
                     "Comment=Convert this OpenSCAD model to a solid STEP file (SCAD2STEP)\n"
                     f"Exec={wrapper} %F --pause\nIcon-Name=document-save-as\nSelection=notnone\n"
                     "Extensions=scad;csg;\nTerminal=true\n")
    _write(files[2], "[Nemo Action]\nName=Live-link to STEP (updates on save)\n"
                     "Comment=Keep a STEP copy of this model updated every time you save it\n"
                     f"Exec={wrapper} %F --watch\nIcon-Name=view-refresh\nSelection=s\n"
                     "Extensions=scad;\nTerminal=true\n")
    term = ('T="$(command -v gnome-terminal || command -v x-terminal-emulator || command -v konsole || command -v xterm)"\n'
            'mapfile -t F <<< "$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS"\n'
            '[ -z "${F[-1]}" ] && unset "F[-1]"\n')
    for path, extra in ((files[3], "--pause"), (files[4], "--watch")):
        _write(path, "#!/bin/bash\n# " + TAG + "\n" + term +
               f'case "$T" in\n  *gnome-terminal) exec "$T" -- "{wrapper}" "${{F[@]}}" {extra};;\n'
               f'  "") exec "{wrapper}" "${{F[@]}}";;\n'
               f'  *) exec "$T" -e "{wrapper}" "${{F[@]}}" {extra};;\nesac\n', exe=True)
    _write(files[5], "[Desktop Entry]\nType=Application\nName=SCAD2STEP\n"
                     "Comment=Convert OpenSCAD models to solid STEP files\n"
                     f"Exec={wrapper} ui --open %f\nIcon=document-save-as\nTerminal=true\n"
                     "Categories=Graphics;Engineering;\nMimeType=application/x-openscad;text/x-openscad;\n")
    print("\nDone. In Nemo (Linux Mint) right-click a .scad file → “Convert to STEP” or\n"
          "“Live-link to STEP”. (If Nemo was open, it may need a restart: nemo -q)\n"
          "In GNOME Files the same entries are under right-click → Scripts.\n"
          "SCAD2STEP is also in your applications menu and in “Open With”.")
    return 0


def windows(remove):
    sendto = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "SendTo")
    a = os.path.join(sendto, "STEP (SCAD2STEP).bat")
    b = os.path.join(sendto, "STEP live link (SCAD2STEP).bat")
    if remove:
        _rm(a)
        _rm(b)
        return 0
    bat = os.path.join(ROOT, "SCAD2STEP (Windows).bat")
    _write(a, f'@echo off\r\nrem {TAG}\r\ncall "{bat}" %* --pause\r\n')
    _write(b, f'@echo off\r\nrem {TAG}\r\ncall "{bat}" %1 --watch\r\n')
    print("\nDone. Right-click a .scad file → Send to → “STEP (SCAD2STEP)”\n"
          "(on Windows 11: Show more options → Send to).")
    return 0


def main(argv):
    remove = "--remove" in argv
    print(("Removing" if remove else "Adding") + " SCAD2STEP file-manager entries…")
    if sys.platform.startswith("win"):
        return windows(remove)
    if sys.platform == "darwin":
        print("macOS: Finder integration isn't available yet. Use the app window, or the command line:\n"
              f'  bash "{os.path.join(HERE, "portable.sh")}" model.scad --watch')
        return 0
    return linux(remove)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
