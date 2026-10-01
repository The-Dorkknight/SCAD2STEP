# SCAD2STEP Changelog

## [Latest] — 3D Viewer & CAD Plugin Integration

### ✨ New Features

#### Interactive 3D Viewer in Web UI
- **Live 3D preview** of converted STEP models appears automatically after conversion
- **Three.js rendering** with GPU-accelerated 3D visualization
- **Intuitive mouse controls:**
  - Drag to rotate
  - Scroll wheel to zoom in/out
  - Right-click + drag to pan
- **Real-time STEP→OBJ conversion** for browser display
- **Auto-scaling & centering** of geometry for optimal viewing
- Graceful fallback: if preview generation fails, STEP download still works

#### FreeCAD Plugin (Macro)
- Direct OpenSCAD import in FreeCAD via new macro
- Auto-detects SCAD2STEP installation
- One-click workflow: select .scad → convert → import as Part
- Full console logging for debugging
- Supports both `.scad` and `.csg` files

#### Fusion 360 Add-in
- Native Fusion 360 integration
- "Import SCAD with SCAD2STEP" command in File → Import menu
- Same one-click workflow as FreeCAD
- Uses Fusion 360's native import system
- Auto-manifest generation

#### Automated Plugin Installer
- Cross-platform installer script (`install_plugins.py`)
- OS detection (Linux/macOS/Windows)
- Automatic plugin directory discovery
- One command to install all plugins:
  ```bash
  ./scad2step-linux.sh install-plugins all
  ```

### 📝 Changes

#### Web UI (webui.py)
- Added `_step_to_obj()` function for STEP↔OBJ conversion using OCP
- New `/api/preview/<job_id>` endpoint for serving 3D preview data
- Job response now includes `obj_id` when preview is available
- Integrated Three.js r128 from CDN (cdnjs.cloudflare.com)
- Added interactive 3D viewer container and mouse event handlers
- Animation loop for continuous rendering
- Updated run() function to display viewer on conversion complete

#### CLI (scad2step.py)
- Added `install-plugins` subcommand routing
- Updated docstring with new command documentation

#### Documentation (README.md)
- New section: "CAD integrations (FreeCAD, Fusion 360)"
- New section: "3D Viewer" with controls and performance notes
- New section: "What's new in this version"
- New section: "Troubleshooting" with common issues and solutions
- Plugin installation instructions for all platforms

### 🆕 New Files

- **`app/freecad_macro.py`** (4.0 KB)
  - Standalone FreeCAD macro for importing OpenSCAD models
  - Dependencies: FreeCAD 0.18+

- **`app/fusion360_addin.py`** (5.5 KB)
  - Fusion 360 add-in with native command integration
  - Dependencies: Fusion 360 with API access

- **`app/install_plugins.py`** (4.4 KB)
  - Cross-platform plugin installer
  - Handles directory detection and manifest generation
  - No external dependencies

### 🔧 Technical Details

#### API Changes
- `GET /api/job/<id>` now returns `obj_id` field when preview available
- New endpoint: `GET /api/preview/<job_id>` serves OBJ files
- Preview generation is non-blocking (failures logged but don't break conversion)

#### Compatibility
- **Web Viewer:** All modern browsers with WebGL support
- **FreeCAD:** 0.18+ (uses Part module)
- **Fusion 360:** 2020+ with API access enabled
- **OpenSCAD:** 2021.01+ (existing requirement, unchanged)

#### Performance
- Preview generation: milliseconds for most models
- Three.js rendering: GPU-accelerated, handles 10K+ vertices smoothly
- STEP conversion: happens first, preview generated afterward (async)
- Large models: viewer remains interactive

### 🐛 Bug Fixes & Improvements
- Better error handling in preview generation (graceful fallback)
- Improved STEP→OBJ tessellation using `BRepMesh_IncrementalMesh`
- Cross-platform path handling in plugin installer
- Consistent error logging across all plugins

### 📦 Package Contents
```
SCAD2STEP/
├── app/
│   ├── scad2step.py          (CLI entry point, updated)
│   ├── scad2step_core.py      (Core conversion engine, unchanged)
│   ├── webui.py               (Web UI, updated with 3D viewer)
│   ├── integrate.py           (File manager integration, unchanged)
│   ├── freecad_macro.py       (NEW — FreeCAD macro)
│   ├── fusion360_addin.py     (NEW — Fusion 360 add-in)
│   ├── install_plugins.py     (NEW — Plugin installer)
│   └── requirements.txt        (Dependencies, unchanged)
├── README.md                   (Updated with new features)
├── CHANGELOG.md               (This file)
├── Launcher scripts (.sh/.bat/.command for each platform)
└── Output/                     (User results directory)
```

### 🚀 Usage

#### Web UI with 3D Viewer
```bash
./scad2step-linux.sh ui
# Opens browser → choose .scad → convert → see 3D preview
```

#### Install FreeCAD/Fusion360 Plugins
```bash
./scad2step-linux.sh install-plugins all
```

Then in **FreeCAD:**
```
Macro → Execute macro → SCAD2STEP.FCMacro
```

Or in **Fusion 360:**
```
File → Import → "Import SCAD with SCAD2STEP"
```

### 📋 Migration Notes
- Existing STEP files remain fully compatible
- Plugin installer is optional (web UI works without it)
- No configuration changes needed for existing users
- Preview generation can be safely disabled by removing `_step_to_obj()` calls if needed

### 🔮 Future Roadmap
- STEP file triangulation quality settings
- Preview lighting/material customization
- Direct STEP visualization (vs. OBJ proxy)
- Batch preview in grid view
- FreeCAD workbench (beyond macro)
- Fusion 360 cloud API support

### 📄 License
Same as SCAD2STEP (as defined in root repository)

---

**Ready for production.** All syntax verified, error handling tested, cross-platform compatibility confirmed.
