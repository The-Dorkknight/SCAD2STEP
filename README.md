# SCAD2STEP — portable

Turns OpenSCAD models (`.scad`, or `.csg` exports) into **solid STEP files with real CAD
geometry**: true cylinders, cones, spheres, arcs and swept surfaces, not a mesh.
The app opens in your web browser, and there is a command line too.

## Start
| System | Double-click |
|---|---|
| macOS 12+ (Apple Silicon or Intel) | `SCAD2STEP (Mac).command` |
| Windows 10/11, 64-bit | `SCAD2STEP (Windows).bat` |
| Linux (x86-64 / 64-bit ARM) | `scad2step-linux.sh` |

A small window opens and your browser shows the app. **Keep that window open while you
work; close it to quit.**

You also need **OpenSCAD** installed (openscad.org; any version from 2021.01 on). The app finds
it by itself: the normal install, an AppImage in your home, Downloads or Applications folder,
the Flatpak, or the Snap. Otherwise enter its location under *Settings*. A `.csg` file
(OpenSCAD: File → Export → Export as CSG) converts even without OpenSCAD, as long as it has
no text, hull of mixed shapes or imported meshes.

**macOS first time:** right-click the `.command` file → Open → Open (or System Settings →
Privacy & Security → "Open Anyway").

## Using it
1. **Choose a .scad on this computer.** Choosing from disk means `include`/`use` libraries
   next to it (and BOSL2 and friends in your library folder) are found, and the live link can
   watch it. Dropping a file into the page works too, for self-contained models.
2. **Parameters:** the file's Customizer variables appear as fields (sliders' ranges,
   drop-downs, `/* [Groups] */` and `/* [Hidden] */` are respected). Change them to export a
   variant without editing the file.
3. **Convert to STEP.** The STEP is saved next to the .scad (or in `Output/`) and offered as
   a download. The report says how many parts became exact CAD, whether the result is a
   closed solid, and how its volume and size compare with OpenSCAD's own render.
4. **3D Preview:** After conversion, an interactive 3D preview of your model appears below.
   Drag to rotate, scroll to zoom, right-drag to pan. The preview is generated in real-time.

### Live link (the "plugin" part)
OpenSCAD has no plugin system, so SCAD2STEP sits next to it instead. Switch **Live link** on
and keep editing in OpenSCAD: every time you save (F2), the STEP beside your .scad is
rebuilt within a second or two, including when a library file it uses changes. Your CAD
program can re-import it whenever you like.

### CAD integrations (FreeCAD, Fusion 360)
Install plugins to import OpenSCAD models directly in your CAD software:

    ./scad2step-linux.sh install-plugins all      # Linux
    "SCAD2STEP (Windows).bat" install-plugins all # Windows
    bash "SCAD2STEP (Mac).command" install-plugins all # macOS

Or install individually:
    
    ./scad2step-linux.sh install-plugins freecad     # FreeCAD macro only
    ./scad2step-linux.sh install-plugins fusion360   # Fusion 360 add-in only

**FreeCAD:** After installation, go to *Macro → Execute macro → SCAD2STEP* (or use recent macros).
A dialog asks for your `.scad` file; it's converted to STEP and imported as a new part.

**Fusion 360:** After installation, go to *File → Import* — SCAD2STEP appears in the import menu.
Select your `.scad` file; it's converted and imported as a new component.

### Right-click menu
Run once:

    ./scad2step-linux.sh integrate            # Linux
    "SCAD2STEP (Windows).bat" integrate       # Windows

- **Linux Mint (Nemo):** right-click a `.scad` → **Convert to STEP** or **Live-link to STEP
  (updates on save)**. GNOME Files: right-click → Scripts. SCAD2STEP also appears in the
  applications menu and in *Open With*.
- **Windows:** right-click → Send to → **STEP (SCAD2STEP)** / **STEP live link (SCAD2STEP)**.
- Remove again with `integrate --remove`. If you move the SCAD2STEP folder, run `integrate` again.

## What becomes what
| OpenSCAD | STEP |
|---|---|
| `cube`, `polyhedron`, `polygon`, `square` | exact flat faces |
| `cylinder`, `sphere`, `circle` | true cylinders / cones / spheres / arcs |
| `cylinder(..., $fn=6)` and any `$fn` ≤ 8 | kept as the polygon you asked for (nut traps stay hexagons) |
| `linear_extrude` (incl. `scale`, `twist`, `center`) | true prisms, ruled cones, smooth twisted lofts |
| `rotate_extrude` (incl. `angle`) | true surfaces of revolution |
| `union`, `difference`, `intersection`, `for`, `color`, `render` | exact solid booleans |
| `translate`, `rotate`, `mirror`, `scale`, `multmatrix`, `resize` | exact (non-uniform scale turns spheres into ellipsoids) |
| `offset(r=…)`, `offset(delta=…)` | exact 2D offsets with true arcs |
| `hull()` of cubes/polyhedra | exact convex solid |
| `hull()` of equal spheres | exact rounded box / capsule / pill |
| `hull()` of parallel cylinders/prisms, 2D `hull()` of circles | exact rounded plate / slot |
| `minkowski()` with a sphere (3D) or circle (2D) | exact rounded offset |
| `text()`, `projection()`, `import("x.svg/.dxf")`, `offset(chamfer=true)` | outlines from OpenSCAD, curves smoothed into splines |
| `import("x.stl")`, `surface()`, other `hull`/`minkowski` | taken over from OpenSCAD's mesh as exact flat facets |

Same-surface faces are merged at the end, so a plate with 100 holes becomes about 106 faces.

## Command line
    ./scad2step-linux.sh model.scad                      # → model.step next to it
    ./scad2step-linux.sh model.scad -o part.step -D width=40 -D 'label="Hi"'
    ./scad2step-linux.sh model.scad --watch              # live link in the terminal
    ./scad2step-linux.sh *.scad --no-verify              # batch, skip the comparison render
    ./scad2step-linux.sh model.scad --units in           # 1 OpenSCAD unit = 1 inch
    ./scad2step-linux.sh model.scad --faceted-fn 0       # make even $fn=6 circles round

(On a Mac: `bash app/portable.sh …`, on Windows: `"SCAD2STEP (Windows).bat" …`)

## 3D Viewer
After each conversion, an interactive 3D preview appears in the web UI showing your STEP geometry.
Use the viewer to inspect your model before downloading:
- **Drag** to rotate the view
- **Scroll** to zoom in/out
- **Right-drag** to pan

The viewer uses Three.js to render the geometry in real-time. Models are converted to OBJ format
for preview; the full STEP file remains your downloadable output (exact CAD geometry).

## Good to know
- **Numbers:** OpenSCAD hands over its evaluated model with 6 significant digits, which is
  about 0.2 µm on a 100 mm part.
- **BOSL2 and similar libraries** build many shapes (rounded cuboids, gears, threads) as
  `polyhedron`s. Those arrive as exact flat facets, just as OpenSCAD models them. More `$fn`
  gives finer facets.
- **"Compare with OpenSCAD's render"** renders the model once more in OpenSCAD. OpenSCAD 2021.01
  can take a while on big models; switch the check off for speed. Newer OpenSCAD builds use
  their much faster Manifold engine automatically.
- The volume difference in the report is usually OpenSCAD's polygon circles and
  sliced twists against the STEP's true shapes: a few tenths of a percent.
- **3D preview:** If preview generation fails (e.g., on very complex models), the STEP download
  still works — the preview is a convenience, not a requirement.

## Troubleshooting

**3D Viewer not showing after conversion:**
- The viewer generates a preview for most models. If it fails, the STEP file is still downloaded correctly.
- Very large or complex models may skip preview generation for speed — the full STEP output is unaffected.
- Check browser console (F12) for JavaScript errors if the viewer doesn't appear.

**FreeCAD macro not working:**
- Ensure SCAD2STEP folder exists in a standard location (~/SCAD2STEP, ~/Documents/SCAD2STEP, etc.)
- Or move SCAD2STEP to one of the default locations and reinstall the macro.
- Check that OpenSCAD is installed and in your PATH.

**Fusion 360 add-in not appearing:**
- Restart Fusion 360 after running the plugin installer.
- Check that the manifest.xml file was created in the add-in folder.
- Use Fusion 360's Add-in Manager to manually enable the SCAD2STEP add-in if needed.

## Portable and self-contained
Everything stays inside this folder (`app/` the program, `runtime/` a private Python 3.12 and
libraries, `Output/` your results). The first start downloads about 450 MB (1.5 GB on disk);
later starts are instant. Your system Python is never used or changed. Move the folder
anywhere and it keeps working. **Uninstall:** delete the folder (and run `integrate --remove`
first if you added the right-click entries). **Reset:** delete `runtime/`.

## What's new in this version
- **3D Viewer in Web UI:** After conversion, an interactive 3D preview of your STEP model appears
  in the browser. Drag to rotate, scroll to zoom, right-drag to pan. The preview uses Three.js
  and is generated from the STEP geometry in real-time.
- **FreeCAD Integration:** Import OpenSCAD models directly in FreeCAD using the new macro.
  Go to *Macro → Execute macro → SCAD2STEP* and select a `.scad` file.
- **Fusion 360 Integration:** Import OpenSCAD models directly in Fusion 360 using the new add-in.
  Go to *File → Import* and use the SCAD2STEP command.
- **Plugin Installer:** Run `./scad2step-linux.sh install-plugins all` (or equivalent on your OS)
  to automatically install both the FreeCAD macro and Fusion 360 add-in.
