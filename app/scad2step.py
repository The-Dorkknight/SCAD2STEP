#!/usr/bin/env python3
"""
SCAD2STEP — OpenSCAD model -> solid STEP.

  python scad2step.py                              # opens the app in your web browser
  python scad2step.py model.scad                   # writes model.step next to it
  python scad2step.py model.scad out.step -D width=40 -D 'label="Hi"'
  python scad2step.py model.scad --watch           # live link: re-export on every save
  python scad2step.py integrate                    # add "Convert to STEP" to your file manager
  python scad2step.py install-plugins all          # install FreeCAD and Fusion 360 plugins
"""
import argparse
import os
import sys

MIN_PY, MAX_PY = (3, 11), (3, 14)

if not (MIN_PY <= sys.version_info[:2] <= MAX_PY):
    sys.exit(f"This app needs Python {MIN_PY[0]}.{MIN_PY[1]}–{MAX_PY[0]}.{MAX_PY[1]} "
             f"(the CAD library has no builds for {sys.version.split()[0]}).\n"
             "Use the start script for your OS — it picks a suitable Python for you.")

try:
    import scad2step_core as core
except ImportError as _e:  # dependencies missing
    sys.exit(f"Missing library: {_e.name or _e}.\n"
             "Run the start script for your OS (it installs everything), or:\n"
             f"  {os.path.basename(sys.executable)} -m pip install -r requirements.txt")

UNITS = {"mm": 1.0, "in": 25.4, "inches": 25.4, "cm": 10.0, "m": 1000.0}


def _defines(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            sys.exit(f"-D needs name=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def run_cli(argv):
    ap = argparse.ArgumentParser(prog="scad2step", description="Convert OpenSCAD (.scad/.csg) to solid STEP.")
    ap.add_argument("input", nargs="+", help=".scad or .csg file(s)")
    ap.add_argument("-o", "--output", help="output .step (single input only; default: next to the input)")
    ap.add_argument("-D", dest="defines", action="append", metavar="NAME=VALUE",
                    help="set a variable, like OpenSCAD's -D (strings need quotes: -D 'txt=\"Hi\"')")
    ap.add_argument("--units", choices=sorted(UNITS), default="mm",
                    help="what one OpenSCAD unit means (STEP is written in mm)")
    ap.add_argument("--faceted-fn", type=int, default=8,
                    help="circles with an explicit $fn at or below this stay polygons (default 8: "
                         "hexagon nut traps stay hexagons); higher $fn becomes truly round")
    ap.add_argument("--no-verify", action="store_true", help="skip the comparison with OpenSCAD's render")
    ap.add_argument("--openscad", default="", help="path to the OpenSCAD program (default: find it)")
    ap.add_argument("--watch", action="store_true",
                    help="keep running; re-export every time the file (or a library it uses) is saved")
    ap.add_argument("--pause", action="store_true", help="wait for Enter before closing (for file-manager use)")
    a = ap.parse_args(argv)
    prm = core.ScadParams(defines=_defines(a.defines), faceted_fn=a.faceted_fn, scale=UNITS[a.units],
                          verify=not a.no_verify, openscad=a.openscad)
    if a.openscad:
        core.save_settings(openscad=a.openscad)
    if a.output and len(a.input) > 1:
        sys.exit("-o/--output works with a single input file")
    code = 0
    try:
        if a.watch:
            if len(a.input) != 1:
                sys.exit("--watch works with a single input file")
            out = a.output or os.path.splitext(a.input[0])[0] + ".step"
            try:
                core.watch(a.input[0], out, prm, log=print)
            except KeyboardInterrupt:
                print("\nStopped watching.")
            return 0
        for inp in a.input:
            out = a.output or os.path.splitext(inp)[0] + ".step"
            print(f"=== {inp}")
            try:
                rep = core.convert(inp, out, prm, log=print)
                print(f"\nSaved {out}\n")
                if not rep.is_solid:
                    code = 2
            except core.ScadError as e:
                print(f"\nERROR: {e}\n")
                code = 1
    finally:
        if a.pause:
            try:
                input("Press Enter to close…")
            except EOFError:
                pass
    return code


def run_ui(argv):
    ap = argparse.ArgumentParser(prog="scad2step.py ui", description="Open the app in your web browser.")
    ap.add_argument("--port", type=int, default=0, help="port to use (default: any free one)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--open", default="", help="start with this .scad selected")
    a = ap.parse_args(argv)
    import webui
    webui.serve(port=a.port, open_browser=not a.no_browser, open_path=a.open)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "ui":
        sys.exit(run_ui(args[1:]))
    if args[0] == "integrate":
        import integrate
        sys.exit(integrate.main(args[1:]))
    if args[0] == "install-plugins":
        import install_plugins
        sys.exit(install_plugins.main(args[1:]))
    sys.exit(run_cli(args))
