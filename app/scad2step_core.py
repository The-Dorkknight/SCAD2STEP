"""
scad2step — convert OpenSCAD models (.scad or .csg) into solid STEP files.

How it works
------------
1. OpenSCAD evaluates the model and writes its CSG tree (.csg). In that file every
   variable, loop, module and function has already been resolved: only primitives,
   transformation matrices, booleans and extrusions are left.
2. That tree is rebuilt as real B-rep geometry in OpenCascade: cubes become boxes,
   cylinders true cylinders/cones, spheres true spheres, circles true arcs, extrusions
   and revolutions true swept surfaces, and booleans exact solid booleans.
   A deliberately low $fn (e.g. $fn=6 for a nut trap) is kept as the polygon it is.
   hull() of polyhedra, of equal spheres (rounded boxes) and of parallel prisms/cylinders
   (rounded plates) is built exactly too, as is minkowski() with a sphere or circle.
3. The rare parts with no exact CAD equivalent (imported STL, surface(), other hulls
   and minkowskis, anything that fails) are rendered by OpenSCAD and taken over as exact
   flat facets, then joined in. Text and 2D imports come in as exact polygons.
4. Same-surface faces are merged, the solid is checked, and a STEP file is written.
   Optionally the result is compared against OpenSCAD's own render.
"""
from __future__ import annotations

import glob
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np


from OCP.gp import (gp_Pnt, gp_Dir, gp_Vec, gp_Ax1, gp_Ax2, gp_Pln, gp_Trsf, gp_GTrsf, gp_Mat,
                    gp_XYZ, gp_Circ, gp_Elips)
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut, BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (BRepBuilderAPI_Transform, BRepBuilderAPI_GTransform,
                                BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import (BRepOffsetAPI_ThruSections, BRepOffsetAPI_MakeOffset,
                               BRepOffsetAPI_MakeOffsetShape)
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeSphere, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakeCone, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol)
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection, GeomAbs_Circle, GeomAbs_Line
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Shape, ShapeFix_Shell
from OCP.ShapeAnalysis import ShapeAnalysis_Shell
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeSolid
from OCP.BRepLib import BRepLib
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs, STEPControl_Reader
from OCP.Interface import Interface_Static
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shell
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_EDGE, TopAbs_WIRE, TopAbs_SHELL, TopAbs_VERTEX
try:  # OCP >= 7.8 / 8.x
    from OCP.collections import List_TopoDS_Shape as TopTools_ListOfShape
except ImportError:  # older OCP
    from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS_Compound

try:
    from OCP.BRepOffset import BRepOffset_Mode
    _SKIN = BRepOffset_Mode.BRepOffset_Skin
except Exception:  # older OCP
    from OCP.BRepOffset import BRepOffset_Skin as _SKIN

I4 = np.eye(4)


# ====================================================================== OCC basics
def _S(cls, name):
    """Static-method compat: OCP exposes statics as Name_s in some versions."""
    return getattr(cls, name + "_s", None) or getattr(cls, name)


def _cast(kind, shape):
    fn = getattr(TopoDS, kind, None) or getattr(TopoDS, kind + "_s")
    return fn(shape)


def _explore(shape, kind):
    ex = TopExp_Explorer(shape, kind)
    while ex.More():
        yield ex.Current()
        ex.Next()


class _Log:
    def __init__(self, log=None, progress=None):
        self._log = log or (lambda s: None)
        self._prog = progress or (lambda f: None)

    def __call__(self, msg):
        self._log(msg)

    def p(self, frac):
        self._prog(max(0.0, min(1.0, frac)))


def _quiet_occt():
    """Stop OpenCascade printing its coloured transfer statistics to the console."""
    try:
        from OCP.Message import Message, Message_Gravity
        for p in _S(Message, "DefaultMessenger")().Printers():
            p.SetTraceLevel(Message_Gravity.Message_Fail)
    except Exception:
        pass


_quiet_occt()


def _ascii_safe(path):
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def write_step(shape, path):
    _S(Interface_Static, "SetCVal")("write.step.unit", "MM")
    try:
        _S(Interface_Static, "SetCVal")("write.step.schema", "AP214IS")
    except Exception:
        pass
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    target, tmp = str(path), None
    if not _ascii_safe(target):          # OpenCascade file I/O can fail on non-ASCII paths
        fd, tmp = tempfile.mkstemp(suffix=".step")
        os.close(fd)
    if w.Write(tmp or target) != IFSelect_RetDone:
        raise IOError(f"Failed to write {path}")
    if tmp:
        shutil.move(tmp, target)


def read_step(path):
    src, tmp = str(path), None
    if not _ascii_safe(src):
        fd, tmp = tempfile.mkstemp(suffix=".step")
        os.close(fd)
        shutil.copyfile(src, tmp)
    try:
        r = STEPControl_Reader()
        if r.ReadFile(tmp or src) != IFSelect_RetDone:
            raise IOError(f"Could not read {path}")
        r.TransferRoots()
        return r.OneShape()
    finally:
        if tmp:
            os.remove(tmp)


def _count_faces(shape):
    return sum(1 for _ in _explore(shape, TopAbs_FACE))


def _sealed(shape):
    """True if every shell is closed (no free edges) and the shape passes the geometry check."""
    for sh in _explore(shape, TopAbs_SHELL):
        sa = ShapeAnalysis_Shell()
        sa.LoadShells(sh)
        sa.CheckOrientedShells(sh, True)
        if sa.HasFreeEdges():
            return False
    return BRepCheck_Analyzer(shape).IsValid()


def _unify(shape, tol, ang=1e-4):
    """Merge faces that lie on the same surface (and edges on the same curve)."""
    try:
        u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
        u.SetLinearTolerance(tol)
        u.SetAngularTolerance(ang)
        u.Build()
        return u.Shape()
    except Exception:
        return shape


def _safe_unify(shape, tol, log):
    """Merge same-surface faces, but never at the cost of a watertight, valid solid."""
    if not _sealed(shape):
        return _unify(shape, tol)
    merged = _unify(shape, tol)
    if _sealed(merged):
        return merged
    log("  (kept some faces unmerged so the solid stays watertight)")
    return shape


# ====================================================================== parameters
@dataclass
class ScadParams:
    defines: dict = field(default_factory=dict)   # name -> OpenSCAD literal, passed as -D
    faceted_fn: int = 8             # a $fn at or below this is kept as a polygon (hex nut traps…)
    fallback_fn: int = 64           # smoothness used when OpenSCAD renders a mesh-only part
    smooth_outlines: bool = True    # text / imported 2D outlines: curves become smooth splines
    scale: float = 1.0              # OpenSCAD units -> mm
    verify: bool = True             # compare with OpenSCAD's own render
    openscad: str = ""              # path to OpenSCAD ("" = find it)
    timeout: float = 900.0          # seconds for each OpenSCAD run
    fuzzy: float = 1e-5             # mm, boolean fuzzy tolerance


@dataclass
class ScadReport:
    ok: bool = False
    out_path: str = ""
    exact_parts: int = 0
    round_parts: int = 0
    mesh_parts: int = 0
    outline_parts: int = 0
    n_faces: int = 0
    n_solids: int = 0
    is_solid: bool = False
    is_valid: bool = False
    step_volume: float = 0.0
    scad_volume: float = 0.0
    size_diff: float = float("nan")
    seconds: float = 0.0
    deps: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def summary(self):
        lines = [f"Built as exact CAD: {self.exact_parts} part(s)"
                 + (f", {self.round_parts} of them with true round surfaces" if self.round_parts else "")
                 + (f";  text/2D imports traced as outlines: {self.outline_parts}" if self.outline_parts else "")
                 + (f";  taken over as flat facets from OpenSCAD's mesh: {self.mesh_parts}" if self.mesh_parts else ""),
                 f"Faces in STEP: {self.n_faces}   solids: {self.n_solids}",
                 f"Closed solid: {'yes' if self.is_solid else 'NO'}   geometry check: "
                 f"{'valid' if self.is_valid else 'has issues'}"]
        if self.step_volume:
            s = f"Volume: STEP {self.step_volume:.2f} mm³"
            if self.scad_volume:
                dv = 100.0 * (self.step_volume - self.scad_volume) / self.scad_volume
                s += f", OpenSCAD render {self.scad_volume:.2f} mm³ ({dv:+.2f}%)"
            lines.append(s)
        if not math.isnan(self.size_diff):
            lines.append(f"Bounding box vs OpenSCAD's render: largest difference {self.size_diff:.4f} mm")
        if self.scad_volume and self.round_parts:
            lines.append("(OpenSCAD approximates circles and twists with flat facets; the STEP has "
                         "the true shapes, so a small difference is expected)")
        for w in self.warnings:
            lines.append("Note: " + w)
        lines.append(f"Time: {self.seconds:.1f} s")
        return "\n".join(lines)


class ScadError(Exception):
    pass


class _NeedMesh(Exception):
    """Raised when a node can't be built exactly and should be rendered by OpenSCAD."""


# ====================================================================== finding OpenSCAD
def _settings_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "settings.json")


def load_settings():
    import json
    try:
        with open(_settings_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(**kw):
    import json
    s = load_settings()
    s.update(kw)
    try:
        with open(_settings_path(), "w") as f:
            json.dump(s, f, indent=1)
    except Exception:
        pass


def _works(cmd):
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=60,
                           **_nowindow())
        return "openscad" in (r.stdout + r.stderr).lower()
    except Exception:
        return False


def _nowindow():
    if sys.platform.startswith("win"):
        return {"creationflags": 0x08000000}      # CREATE_NO_WINDOW
    return {}


def find_openscad(hint=""):
    """Command (list) that runs OpenSCAD, or None."""
    cands = []
    for h in (hint, os.environ.get("OPENSCAD", ""), load_settings().get("openscad", "")):
        if h:
            if h.startswith("flatpak:"):
                cands.append(["flatpak", "run", "--filesystem=host", h[8:]])
            else:
                if os.path.isdir(h) and h.endswith(".app"):
                    h = os.path.join(h, "Contents", "MacOS", "OpenSCAD")
                if sys.platform.startswith("win") and h.lower().endswith(".exe"):
                    com = h[:-4] + ".com"                  # console twin gives us messages
                    if os.path.exists(com):
                        cands.append([com])
                cands.append([h])
    for name in ("openscad", "openscad-nightly", "OpenSCAD"):
        p = shutil.which(name)
        if p:
            cands.append([p])
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        for app in (glob.glob("/Applications/OpenSCAD*.app") + glob.glob(home + "/Applications/OpenSCAD*.app")):
            cands.append([os.path.join(app, "Contents", "MacOS", "OpenSCAD")])
    elif sys.platform.startswith("win"):
        for base in {os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "") + r"\Programs"}:
            for d in sorted(glob.glob(os.path.join(base, "OpenSCAD*")), reverse=True):
                for exe in ("openscad.com", "openscad.exe"):
                    if os.path.exists(os.path.join(d, exe)):
                        cands.append([os.path.join(d, exe)])
    else:
        for p in ("/snap/bin/openscad", "/usr/local/bin/openscad"):
            if os.path.exists(p):
                cands.append([p])
        for d in (home + "/Applications", home + "/Downloads", home + "/bin", home + "/.local/bin", home):
            for p in sorted(glob.glob(os.path.join(d, "OpenSCAD*.AppImage")), reverse=True):
                cands.append([p])
        if shutil.which("flatpak"):
            for app in ("org.openscad.OpenSCAD", "org.openscad.OpenSCAD.Nightly"):
                try:
                    if subprocess.run(["flatpak", "info", app], capture_output=True, timeout=20).returncode == 0:
                        cands.append(["flatpak", "run", "--filesystem=host", app])
                except Exception:
                    pass
    seen = set()
    for c in cands:
        k = tuple(c)
        if k in seen:
            continue
        seen.add(k)
        if (c[0] == "flatpak" or os.path.exists(c[0])) and _works(c):
            return c
    return None


class OpenSCAD:
    def __init__(self, cmd, timeout, log):
        self.cmd, self.timeout, self.log = cmd, timeout, log

    _fast = None

    def fast_backend(self):
        """Newer OpenSCAD builds have the (much faster) Manifold geometry engine."""
        if self._fast is None:
            try:
                r = subprocess.run(list(self.cmd) + ["--help"], capture_output=True, text=True,
                                   timeout=60, **_nowindow())
                self._fast = "manifold" in (r.stdout + r.stderr).lower()
            except Exception:
                self._fast = False
        return self._fast

    def run(self, src, out, defines=None, deps=None, quiet=False):
        out, src = os.path.abspath(out), os.path.abspath(src)
        args = list(self.cmd) + ["-o", out]
        if not out.endswith(".csg") and self.fast_backend():
            args.append("--backend=manifold")
        if deps:
            args += ["-d", deps]
        for k, v in (defines or {}).items():
            args += ["-D", f"{k}={v}"]
        args.append(src)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout,
                               cwd=os.path.dirname(os.path.abspath(src)), **_nowindow())
        except subprocess.TimeoutExpired:
            raise ScadError(f"OpenSCAD took longer than {self.timeout:.0f} s and was stopped")
        msg = (r.stdout or "") + (r.stderr or "")
        for line in msg.splitlines():
            if not quiet and re.match(r"\s*(ECHO|WARNING|ERROR|TRACE|DEPRECATED)", line):
                self.log("  OpenSCAD: " + line.strip())
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            errs = [l for l in msg.splitlines() if "ERROR" in l or "error" in l]
            raise ScadError("OpenSCAD failed: " + ("; ".join(errs[-3:]) or msg.strip()[-400:]
                                                     or f"exit code {r.returncode}"))
        return msg


# ====================================================================== CSG parser
class Node:
    __slots__ = ("name", "args", "children", "mod", "start", "end")

    def __init__(self, name, args, children, mod, start, end):
        self.name, self.args, self.children, self.mod = name, args, children, mod
        self.start, self.end = start, end

    def __repr__(self):
        return f"{self.mod}{self.name}({len(self.children)})"


_TOK = re.compile(r"""
 (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
|(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?inf\b|[-+]?nan\b)
|(?P<str>"(?:[^"\\]|\\.)*")
|(?P<id>\$?[A-Za-z_][A-Za-z0-9_]*)
|(?P<p>[()\[\]{},;=%\#!*:])
""", re.S | re.X)


def _tokens(text):
    out, pos = [], 0
    for m in _TOK.finditer(text):
        if m.start() != pos:
            raise ScadError(f"Unexpected character in CSG at offset {pos}: {text[pos:pos + 20]!r}")
        pos = m.end()
        k = m.lastgroup
        if k == "ws":
            continue
        out.append((k, m.group(), m.start(), m.end()))
    if pos != len(text):
        raise ScadError(f"Unexpected character in CSG at offset {pos}")
    return out


def parse_csg(text):
    toks = _tokens(text)
    i = 0

    def peek():
        return toks[i] if i < len(toks) else ("eof", "", len(text), len(text))

    def take(val=None):
        nonlocal i
        t = peek()
        if val is not None and t[1] != val:
            raise ScadError(f"CSG parse error near offset {t[2]}: expected {val!r}, got {t[1]!r}")
        i += 1
        return t

    def value():
        t = peek()
        if t[0] == "num":
            take()
            return float(t[1])
        if t[0] == "str":
            take()
            return bytes(t[1][1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8") \
                if "\\" in t[1] else t[1][1:-1]
        if t[0] == "id":
            take()
            return {"true": True, "false": False}.get(t[1], None)
        if t[1] == "[":
            take()
            vals = []
            while peek()[1] != "]":
                vals.append(value())
                if peek()[1] in (",", ":"):
                    take()
            take("]")
            return vals
        raise ScadError(f"CSG parse error near offset {t[2]}: unexpected {t[1]!r}")

    def statements(top):
        out = []
        while True:
            t = peek()
            if t[0] == "eof":
                if not top:
                    raise ScadError("CSG ended unexpectedly")
                return out
            if t[1] == "}":
                if top:
                    raise ScadError("Unbalanced '}' in CSG")
                return out
            if t[1] == ";":
                take()
                continue
            start = t[2]
            mod = ""
            while peek()[1] in ("%", "#", "!", "*"):
                mod += take()[1]
            name = take()[1]
            take("(")
            args, pos_i = {}, 0
            while peek()[1] != ")":
                if peek()[0] == "id" and i + 1 < len(toks) and toks[i + 1][1] == "=":
                    k = take()[1]
                    take("=")
                    args[k] = value()
                else:
                    args[pos_i] = value()
                    pos_i += 1
                if peek()[1] == ",":
                    take()
            take(")")
            children = []
            if peek()[1] == "{":
                take("{")
                children = statements(False)
                end = take("}")[3]
            else:
                end = take(";")[3] if peek()[1] == ";" else peek()[2]
            out.append(Node(name, args, children, mod, start, end))

    return statements(True)


# ====================================================================== small helpers
def _num(v, d=0.0):
    try:
        if v is None or isinstance(v, bool):
            return d
        return float(v)
    except Exception:
        return d


def _vec(v, n, d=0.0):
    if isinstance(v, list):
        out = [_num(x, d) for x in v[:n]]
        return out + [d] * (n - len(out))
    x = _num(v, d)
    return [x] * n


def _fragments(r, a):
    fn, fs, fa = _num(a.get("$fn")), _num(a.get("$fs"), 2.0), _num(a.get("$fa"), 12.0)
    if r < 1e-10:
        return 3
    if fn > 0:
        return max(int(fn), 3)
    return int(math.ceil(max(min(360.0 / max(fa, 0.01), r * 2 * math.pi / max(fs, 0.01)), 5)))


def _similarity(M, dims=3):
    """(scale, R, t) if the linear part is rotation(+mirror) x uniform scale, else None."""
    L = M[:dims, :dims]
    U, S, Vt = np.linalg.svd(L)
    if S.min() < 1e-12 or S.max() / S.min() - 1.0 > 2e-4:
        return None
    s = float(np.prod(S) ** (1.0 / dims))
    return s, U @ Vt, M[:dims, 3]


def _trsf(M):
    sim = _similarity(M)
    if sim is None:
        return None
    s, R, t = sim
    A = s * R
    tr = gp_Trsf()
    tr.SetValues(*(float(x) for x in (A[0, 0], A[0, 1], A[0, 2], t[0],
                                        A[1, 0], A[1, 1], A[1, 2], t[1],
                                        A[2, 0], A[2, 1], A[2, 2], t[2])))
    return tr


def _apply(shape, M):
    """Apply a 4x4 affine matrix to a shape; keeps analytic surfaces when possible."""
    if shape is None or np.allclose(M, I4, atol=1e-12):
        return shape
    tr = _trsf(M)
    if tr is not None:
        return BRepBuilderAPI_Transform(shape, tr, True).Shape()
    if abs(np.linalg.det(M[:3, :3])) < 1e-12:
        return None                                   # flattened to nothing
    g = gp_GTrsf()
    L = M[:3, :3]
    g.SetVectorialPart(gp_Mat(*(float(x) for x in L.ravel())))
    g.SetTranslationPart(gp_XYZ(*(float(x) for x in M[:3, 3])))
    return BRepBuilderAPI_GTransform(shape, g, True).Shape()


def _xf(M, P):
    P = np.asarray(P, float)
    if P.shape[1] == 2:
        P = np.c_[P, np.zeros(len(P))]
    return P @ M[:3, :3].T + M[:3, 3]


def _gp(p):
    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)


def _compound(shapes):
    shapes = [s for s in shapes if s is not None]
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    b = BRep_Builder()
    c = TopoDS_Compound()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _faces(shape):
    return [] if shape is None else [_cast("Face", f) for f in _explore(shape, TopAbs_FACE)]


def _solids(shape):
    return [] if shape is None else [_cast("Solid", s) for s in _explore(shape, TopAbs_SOLID)]


def _is_empty(shape):
    return shape is None or not any(True for _ in _explore(shape, TopAbs_FACE))


def _volume(shape):
    p = GProp_GProps()
    _S(BRepGProp, "VolumeProperties")(shape, p)
    return p.Mass()


def _area(shape):
    p = GProp_GProps()
    _S(BRepGProp, "SurfaceProperties")(shape, p)
    return p.Mass()


def _bbox(shape):
    b = Bnd_Box()
    try:
        _S(BRepBndLib, "AddOptimal")(shape, b, False, False)
    except Exception:
        _S(BRepBndLib, "Add")(shape, b)
    p0, p1 = b.CornerMin(), b.CornerMax()
    return np.array([p0.X(), p0.Y(), p0.Z()]), np.array([p1.X(), p1.Y(), p1.Z()])


def _lst(shapes):
    ls = TopTools_ListOfShape()
    for s in shapes:
        ls.Append(s)
    return ls


# ---------------------------------------------------------------- polygon helpers (2D)
def _sarea(P):
    return 0.5 * float(np.sum(P[:, 0] * np.roll(P[:, 1], -1) - np.roll(P[:, 0], -1) * P[:, 1]))


def _inside(pt, P):
    x, y = pt
    xi, yi = P[:, 0], P[:, 1]
    xj, yj = np.roll(xi, 1), np.roll(yi, 1)
    c = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / np.where(yj - yi == 0, 1e-300, yj - yi) + xi)
    return bool(np.count_nonzero(c) % 2)


def _clean_loop(P, eps):
    P = np.asarray(P, float)[:, :2]
    if len(P) > 1 and np.linalg.norm(P[0] - P[-1]) <= eps:
        P = P[:-1]
    keep = [0]
    for k in range(1, len(P)):
        if np.linalg.norm(P[k] - P[keep[-1]]) > eps:
            keep.append(k)
    P = P[keep]
    if len(P) > 2 and np.linalg.norm(P[0] - P[-1]) <= eps:
        P = P[:-1]
    return P


def _wire_from_pts(P):
    mp = BRepBuilderAPI_MakePolygon()
    for p in P:
        mp.Add(_gp(p))
    mp.Close()
    return mp.Wire() if mp.IsDone() else None


def _wire_pts(wire, per_curve=24):
    """Ordered sample points (2D) along a wire."""
    pts = []
    ex = BRepTools_WireExplorer(wire)
    while ex.More():
        e = ex.Current()
        c = BRepAdaptor_Curve(e)
        u0, u1 = c.FirstParameter(), c.LastParameter()
        n = 1 if c.GetType() == GeomAbs_Line else per_curve
        us = np.linspace(u0, u1, n + 1)[:-1]
        if e.Orientation() == 1:   # TopAbs_REVERSED
            us = np.linspace(u1, u0, n + 1)[:-1]
        for u in us:
            p = c.Value(float(u))
            pts.append((p.X(), p.Y()))
        ex.Next()
    return np.array(pts) if pts else np.zeros((0, 2))


def _faces_from_wires(wires, pts_list=None):
    """Planar (XY) faces from closed wires using even-odd nesting (like OpenSCAD polygons)."""
    items = []
    for k, w in enumerate(wires):
        P = pts_list[k] if pts_list is not None else _wire_pts(w)
        if len(P) < 3:
            continue
        a = _sarea(P)
        if abs(a) < 1e-12:
            continue
        items.append([w, P, a])
    if not items:
        return None
    order = sorted(range(len(items)), key=lambda k: -abs(items[k][2]))
    items = [items[k] for k in order]
    depth, parent = [0] * len(items), [-1] * len(items)
    for i in range(len(items)):
        P = items[i][1]
        probe = P[0]
        for j in range(i):
            if _inside(probe, items[j][1]) or _inside(P[len(P) // 2], items[j][1]):
                depth[i] += 1
                parent[i] = j        # sorted by area, so the last hit is the smallest container
    faces = []
    pln = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    for i, (w, P, a) in enumerate(items):
        if depth[i] % 2:
            continue
        outer = w if a > 0 else _cast("Wire", w.Reversed())
        mf = BRepBuilderAPI_MakeFace(pln, outer, True)
        if not mf.IsDone():
            continue
        for j, (w2, P2, a2) in enumerate(items):
            if parent[j] == i and depth[j] % 2:
                mf.Add(w2 if a2 < 0 else _cast("Wire", w2.Reversed()))
        fx = ShapeFix_Face(mf.Face())
        fx.Perform()
        faces.append(fx.Face())
    return _compound(faces)


def _smooth_wire(P, corner_deg=35.0, jump=2.5):
    """Closed polyline (a traced curve, e.g. a glyph outline) -> wire of straight lines and
    smooth splines. Splits at sharp corners and where short curve segments meet long
    straight ones; everything between becomes one interpolating B-spline."""
    from OCP.GeomAPI import GeomAPI_Interpolate
    try:
        from OCP.collections import HArray1_gp_Pnt as TColgp_HArray1OfPnt
    except ImportError:
        from OCP.TColgp import TColgp_HArray1OfPnt
    n = len(P)
    if n < 6:
        return None
    seg = np.roll(P, -1, axis=0) - P                    # segment i: P[i] -> P[i+1]
    L = np.linalg.norm(seg, axis=1)
    if L.min() <= 0:
        return None
    d = seg / L[:, None]
    prev = np.roll(d, 1, axis=0)                          # segment arriving at P[i]
    turn = np.degrees(np.arccos(np.clip((prev * d).sum(1), -1, 1)))
    Lp = np.roll(L, 1)
    ratio = np.maximum(L, Lp) / np.minimum(L, Lp)
    brk = (turn > corner_deg) | (ratio > jump)
    idx = list(np.flatnonzero(brk))
    if not idx:
        idx = [0, n // 4, n // 2, 3 * n // 4]
    # a curved run must be made of short segments; a single segment between breaks is a line
    verts = {}

    def v(i):
        if i not in verts:
            verts[i] = BRepBuilderAPI_MakeVertex(_gp(P[i])).Vertex()
        return verts[i]
    mw = BRepBuilderAPI_MakeWire()
    for k, i0 in enumerate(idx):
        i1 = idx[(k + 1) % len(idx)]
        run = [(i0 + t) % n for t in range(((i1 - i0) % n) or n)] + [i1]
        edges = []
        if len(run) >= 4:
            try:
                arr = TColgp_HArray1OfPnt(1, len(run))
                for t, j in enumerate(run):
                    arr.SetValue(t + 1, _gp(P[j]))
                it = GeomAPI_Interpolate(arr, False, 1e-9)
                it.Perform()
                if it.IsDone():
                    me = BRepBuilderAPI_MakeEdge(it.Curve(), v(run[0]), v(run[-1]))
                    if me.IsDone():
                        edges = [me.Edge()]
            except Exception:
                edges = []
        if not edges:
            for a_, b_ in zip(run[:-1], run[1:]):
                me = BRepBuilderAPI_MakeEdge(v(a_), v(b_))
                if not me.IsDone():
                    return None
                edges.append(me.Edge())
        for e in edges:
            mw.Add(e)
    return mw.Wire() if mw.IsDone() else None


def _faces_from_loops(loops, eps=1e-9):
    wires, pts = [], []
    for P in loops:
        P = _clean_loop(P, eps)
        if len(P) < 3 or abs(_sarea(P)) < 1e-12:
            continue
        w = _wire_from_pts(P)
        if w is not None:
            wires.append(w)
            pts.append(P)
    return _faces_from_wires(wires, pts)


# ---------------------------------------------------------------- exact 2D hull of disks
def _hull_generators(shape):
    """Disks (cx, cy, r) and points (r=0) whose convex hull equals the shape's hull."""
    gens = []
    for e in _explore(shape, TopAbs_EDGE):
        c = BRepAdaptor_Curve(_cast("Edge", e))
        u0, u1 = c.FirstParameter(), c.LastParameter()
        if c.GetType() == GeomAbs_Circle:
            ci = c.Circle()
            if abs(u1 - u0) >= 2 * math.pi - 1e-7:
                p = ci.Location()
                gens.append((p.X(), p.Y(), ci.Radius()))
                continue
        n = 1 if c.GetType() == GeomAbs_Line else 64
        for u in np.linspace(u0, u1, n + 1):
            p = c.Value(float(u))
            gens.append((p.X(), p.Y(), 0.0))
    if not gens:
        return []
    g = np.round(np.array(gens), 9)
    return [tuple(x) for x in np.unique(g, axis=0)]


def _chain_hull(P):
    idx = np.lexsort((P[:, 1], P[:, 0]))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for i in idx:
        while len(lower) >= 2 and cross(P[lower[-2]], P[lower[-1]], P[i]) <= 0:
            lower.pop()
        lower.append(i)
    for i in idx[::-1]:
        while len(upper) >= 2 and cross(P[upper[-2]], P[upper[-1]], P[i]) <= 0:
            upper.pop()
        upper.append(i)
    return lower[:-1] + upper[:-1]          # counter-clockwise


def hull2d(shape):
    gens = _hull_generators(shape)
    if not gens:
        return None
    pts, lab = [], []
    th = np.linspace(0, 2 * math.pi, 720, endpoint=False)
    for k, (x, y, r) in enumerate(gens):
        if r > 0:
            pts.append(np.c_[x + r * np.cos(th), y + r * np.sin(th)])
            lab += [k] * len(th)
        else:
            pts.append(np.array([[x, y]]))
            lab.append(k)
    P = np.vstack(pts)
    lab = np.array(lab)
    h = _chain_hull(P)
    seq = [int(lab[i]) for i in h]
    seq = [s for i, s in enumerate(seq) if s != seq[i - 1]] or seq[:1]
    if len(seq) == 1:
        x, y, r = gens[seq[0]]
        if r <= 0:
            return None
        e = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(x, y, 0), gp_Dir(0, 0, 1)), r)).Edge()
        return _faces_from_wires([BRepBuilderAPI_MakeWire(e).Wire()])
    if len(seq) == 2 and all(gens[s][2] == 0 for s in seq):
        return None
    # exact outer tangents between consecutive generators
    tang = []
    for i in range(len(seq)):
        (x1, y1, r1), (x2, y2, r2) = gens[seq[i]], gens[seq[(i + 1) % len(seq)]]
        d = math.hypot(x2 - x1, y2 - y1)
        if d < 1e-12 or abs(r1 - r2) >= d:
            raise _NeedMesh("degenerate hull")
        u = np.array([(x2 - x1) / d, (y2 - y1) / d])
        w = np.array([u[1], -u[0]])
        a = (r1 - r2) / d
        m = a * u + math.sqrt(max(0.0, 1 - a * a)) * w
        tang.append((np.array([x1, y1]) + r1 * m, np.array([x2, y2]) + r2 * m))
    mw = BRepBuilderAPI_MakeWire()
    n = len(seq)
    for i in range(n):
        x, y, r = gens[seq[i]]
        p_in, p_out = tang[i - 1][1], tang[i][0]
        if r > 0 and np.linalg.norm(p_in - p_out) > 1e-9:
            circ = gp_Circ(gp_Ax2(gp_Pnt(x, y, 0), gp_Dir(0, 0, 1)), r)
            mw.Add(BRepBuilderAPI_MakeEdge(circ, _gp(p_in), _gp(p_out)).Edge())
        a_, b_ = tang[i]
        if np.linalg.norm(a_ - b_) > 1e-9:
            mw.Add(BRepBuilderAPI_MakeEdge(_gp(a_), _gp(b_)).Edge())
    if not mw.IsDone():
        raise _NeedMesh("hull wire")
    return _faces_from_wires([mw.Wire()])


# ---------------------------------------------------------------- polyhedra / meshes
def polyhedron_solid(V, faces, log=lambda *a: None):
    """Exact solid(s) from points + polygon faces. Planar polygons stay single faces,
    warped ones are split into triangles. Edges are shared between faces directly
    (no tolerance-based sewing), so the result is exactly the input."""
    V = np.asarray(V, float)
    if len(V) == 0:
        return None
    size = float(np.linalg.norm(V.max(0) - V.min(0))) or 1.0
    q = 1e-9 * max(size, 1.0)
    key = np.round(V / q).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    U = V[first]
    polys = []
    for f in faces:
        idx = [int(inv[int(x)]) for x in f]
        idx = [x for k, x in enumerate(idx) if x != idx[k - 1]]
        if len(idx) < 3 or len(set(idx)) != len(idx):
            if len(set(idx)) >= 3 and len(set(idx)) != len(idx):
                polys += [[idx[0], idx[k], idx[k + 1]] for k in range(1, len(idx) - 1)]
            continue
        if len(idx) > 3:
            P = U[idx]
            c = P.mean(0)
            _, _, vt = np.linalg.svd(P - c)
            # OpenSCAD writes 6 significant digits, so a flat quad can be off by ~1e-6
            if np.abs((P - c) @ vt[-1]).max() > 2e-5 * size:
                polys += [[idx[0], idx[k], idx[k + 1]] for k in range(1, len(idx) - 1)]
                continue
        polys.append(idx)
    vtx, edges, occ = {}, {}, []

    def vert(i):
        if i not in vtx:
            vtx[i] = BRepBuilderAPI_MakeVertex(_gp(U[i])).Vertex()
        return vtx[i]

    def edge(i, j):
        k = (i, j) if i < j else (j, i)
        if k not in edges:
            me = BRepBuilderAPI_MakeEdge(vert(k[0]), vert(k[1]))
            edges[k] = me.Edge() if me.IsDone() else None
        e = edges[k]
        if e is None:
            return None
        return e if i < j else _cast("Edge", e.Reversed())

    pi = 0
    while pi < len(polys):
        poly = polys[pi]
        pi += 1
        P = U[poly]
        if len(poly) == 3 and np.linalg.norm(np.cross(P[1] - P[0], P[2] - P[0])) <= (1e-12 * size) ** 2:
            continue
        mw = BRepBuilderAPI_MakeWire()
        ok = True
        for k in range(len(poly)):
            e = edge(poly[k], poly[(k + 1) % len(poly)])
            if e is None:
                ok = False
                break
            mw.Add(e)
        if not ok or not mw.IsDone():
            continue
        mf = BRepBuilderAPI_MakeFace(mw.Wire(), True)
        if not mf.IsDone() and len(poly) > 3:
            # nearly flat: put it on the best-fit plane (edges stay shared, tolerance absorbs the rest)
            c = P.mean(0)
            _, _, vt = np.linalg.svd(P - c)
            nrm = vt[-1] if np.cross(P[1] - P[0], P[2] - P[0]) @ vt[-1] >= 0 else -vt[-1]
            mf = BRepBuilderAPI_MakeFace(gp_Pln(_gp(c), gp_Dir(*map(float, nrm))), mw.Wire(), True)
            if mf.IsDone():
                fx = ShapeFix_Face(mf.Face())
                fx.Perform()
                occ.append(fx.Face())
                continue
        if mf.IsDone():
            occ.append(mf.Face())
        elif len(poly) > 3:        # not flat enough for one face: split into triangles
            polys += [[poly[0], poly[j], poly[j + 1]] for j in range(1, len(poly) - 1)]
    if not occ:
        return None
    b = BRep_Builder()
    shell = TopoDS_Shell()
    b.MakeShell(shell)
    for f in occ:
        b.Add(shell, f)
    fx = ShapeFix_Shell(shell)
    fx.FixFaceOrientation(shell)
    fixed = fx.Shape()
    solids, n_open = [], 0
    for sh in _explore(fixed, TopAbs_SHELL):
        sh = _cast("Shell", sh)
        if not _S(BRep_Tool, "IsClosed")(sh):
            n_open += 1
            continue
        ms = BRepBuilderAPI_MakeSolid(sh)
        if not ms.IsDone():
            n_open += 1
            continue
        so = ms.Solid()
        try:
            _S(BRepLib, "OrientClosedSolid")(so)
        except Exception:
            pass
        solids.append(so)
    if not solids:
        raise _NeedMesh("the polyhedron is not closed")
    if n_open:
        log("  Warning: part of a polyhedron/mesh is not closed and was left out")
    res = _compound(solids)
    return _unify(res, 1e-7 * size, 1e-6) if len(occ) > 12 else res


def read_off(path):
    """Vertices and polygon faces of an OFF file (as written by OpenSCAD)."""
    lines = []
    for ln in open(path, encoding="utf-8", errors="replace"):
        ln = ln.split("#", 1)[0].strip()
        if ln:
            lines.append(ln)
    if not lines:
        return np.zeros((0, 3)), []
    head = lines[0].split()
    k = 1
    if head[0].upper().endswith("OFF"):
        nums = head[1:]
        if len(nums) < 2:
            nums = lines[1].split()
            k = 2
    else:
        nums = head
    nv, nf = int(nums[0]), int(nums[1])
    V = np.array([[float(x) for x in lines[k + i].split()[:3]] for i in range(nv)]).reshape(-1, 3)
    F = []
    for i in range(nf):
        t = lines[k + nv + i].split()
        n = int(t[0])
        F.append([int(x) for x in t[1:1 + n]])
    return V, F


def mesh_volume(V, F):
    vol = 0.0
    for f in F:
        a = V[f[0]]
        for k in range(1, len(f) - 1):
            vol += float(np.dot(a, np.cross(V[f[k]], V[f[k + 1]])))
    return vol / 6.0


def _convex_hull_solid(P):
    from scipy.spatial import ConvexHull
    P = np.asarray(P, float)
    if len(P) < 4:
        return None
    try:
        h = ConvexHull(P)
    except Exception:
        return None                                    # flat: the hull has no volume
    return polyhedron_solid(P, h.simplices.tolist())


def _frame_to_z(a):
    """4x4 rotation taking direction a onto +Z (and its inverse)."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    t = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
    x = np.cross(t, a)
    x /= np.linalg.norm(x)
    y = np.cross(a, x)
    R = np.eye(4)
    R[:3, :3] = np.array([x, y, a])
    return R, R.T


# ====================================================================== the builder
_2D = {"square", "circle", "polygon", "text", "projection", "offset", "fill"}
_3D = {"cube", "sphere", "cylinder", "polyhedron", "linear_extrude", "rotate_extrude",
       "surface", "roof"}
_PASS = {"group", "union", "color", "render", "root", "intersection_for", ""}
MESH_EXT = (".stl", ".off", ".obj", ".3mf", ".amf", ".ply")


class Builder:
    def __init__(self, text, prm: ScadParams, log, src_dir, oscad: OpenSCAD | None, tmp):
        self.text, self.prm, self.log = text, prm, log
        self.src_dir, self.oscad, self.tmp = src_dir, oscad, tmp
        self.rep = None
        self._dim = {}
        self.n_sub = 0

    # ------------------------------------------------------------ bookkeeping
    def dim(self, n):
        k = id(n)
        if k in self._dim:
            return self._dim[k]
        if "%" in n.mod or "*" in n.mod:
            d = 0
        elif n.name in _2D:
            d = 2
        elif n.name in _3D:
            d = 3
        elif n.name == "import":
            f = str(n.args.get("file", n.args.get(0, "")))
            d = 2 if f.lower().endswith((".svg", ".dxf")) else 3
        else:
            d = 0
            for c in n.children:
                d = self.dim(c)
                if d:
                    break
            if not n.children and n.name not in _PASS and n.name != "multmatrix":
                d = 3
        self._dim[k] = d
        return d

    def kids(self, n, d):
        out = []
        for c in n.children:
            cd = self.dim(c)
            if cd == d:
                out.append(c)
            elif cd:
                self.warn(f"a {cd}D object inside a {d}D '{n.name}' was ignored (OpenSCAD ignores it too)")
        return out

    def warn(self, msg):
        if msg not in self.rep.warnings:
            self.rep.warnings.append(msg)
            self.log("Note: " + msg)

    # ------------------------------------------------------------ booleans
    def boolean(self, op, args, tools=()):
        args = [s for s in args if not _is_empty(s)]
        tools = [s for s in tools if not _is_empty(s)]
        if op == "fuse":
            if not args:
                return None
            if len(args) == 1 and not tools:
                return args[0]
            args, tools = args[:1], args[1:] + list(tools)
        if op == "cut":
            if not args:
                return None
            if not tools:
                return _compound(args)
        last = None
        for fz in (self.prm.fuzzy, self.prm.fuzzy * 100):
            algo = {"fuse": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[op]()
            algo.SetArguments(_lst(args))
            algo.SetTools(_lst(tools))
            algo.SetFuzzyValue(fz)
            algo.SetRunParallel(True)
            algo.SetNonDestructive(True)
            algo.Build()
            if not algo.IsDone() or (hasattr(algo, "HasErrors") and algo.HasErrors()):
                last = "boolean failed"
                continue
            res = algo.Shape()
            if _is_empty(res):
                return None
            if not BRepCheck_Analyzer(res).IsValid():
                fx = ShapeFix_Shape(res)
                fx.Perform()
                res = fx.Shape()
                if not BRepCheck_Analyzer(res).IsValid():
                    last = "boolean gave invalid geometry"
                    continue
            return _unify(res, max(1e-6, self.prm.fuzzy))
        raise _NeedMesh(last or "boolean failed")

    # ------------------------------------------------------------ 3D
    def solid(self, n, M):
        try:
            return self._solid(n, M)
        except _NeedMesh as e:
            why = str(e)
        except ScadError:
            raise
        except Exception as e:  # noqa — any OCC failure: let OpenSCAD render this part
            why = f"{e.__class__.__name__}: {e}"
        if not n.children and n.name in ("cube", "sphere", "cylinder"):
            raise ScadError(f"Could not build {n.name}: {why}")
        self.log(f"  '{n.name}' needs the mesh route ({why})")
        return self.render_mesh(n, M)

    def _solid(self, n, M):
        a, nm = n.args, n.name
        if "%" in n.mod or "*" in n.mod:
            return None
        if nm == "multmatrix":
            m = np.array(a.get("m", a.get(0)), float)
            return self.union3([c for c in self.kids(n, 3)], M @ m)
        if nm in _PASS or nm == "union":
            return self.union3(self.kids(n, 3), M)
        if nm == "difference":
            ks = self.kids(n, 3)
            if not ks:
                return None
            base = self.solid(ks[0], M)
            if base is None:
                return None
            return self.boolean("cut", [base], [self.solid(k, M) for k in ks[1:]])
        if nm == "intersection":
            ks = self.kids(n, 3)
            shapes = [self.solid(k, M) for k in ks]
            if not shapes or any(s is None for s in shapes):
                return None
            res = shapes[0]
            for s in shapes[1:]:
                res = self.boolean("common", [res], [s])
                if res is None:
                    return None
            return res
        if nm == "cube":
            return self.cube(a, M)
        if nm == "sphere":
            return self.sphere(a, M)
        if nm == "cylinder":
            return self.cylinder(a, M)
        if nm == "polyhedron":
            pts = np.array(a.get("points", []), float)
            fs = a.get("faces", a.get("triangles", []))
            if len(pts) == 0 or not fs:
                return None
            W = _xf(M, pts[:, :3])
            self.rep.exact_parts += 1
            return polyhedron_solid(W, fs, self.log)
        if nm == "linear_extrude":
            return _apply(self.linear_extrude(n), M)
        if nm == "rotate_extrude":
            return _apply(self.rotate_extrude(n), M)
        if nm == "minkowski":
            return self.minkowski3(n, M)
        if nm == "resize":
            return self.resize(n, M, 3)
        if nm == "hull":
            return self.hull3(n, M)
        # import, surface, roof, other hulls/minkowskis, unknown — OpenSCAD renders it
        raise _NeedMesh(nm)

    def union3(self, kids, M):
        shapes = [self.solid(k, M) for k in kids]
        return self.boolean("fuse", shapes)

    def cube(self, a, M):
        s = _vec(a.get("size", 1), 3, 1.0)
        if min(s) <= 0:
            return None
        c = np.array(s) / 2 if a.get("center") else np.zeros(3)
        P = np.array([[x, y, z] for z in (0, s[2]) for y in (0, s[1]) for x in (0, s[0])]) - c
        F = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]]
        self.rep.exact_parts += 1
        return polyhedron_solid(_xf(M, P), F)

    def _faceted(self, a):
        fn = _num(a.get("$fn"))
        return 0 < fn <= self.prm.faceted_fn

    def sphere(self, a, M):
        r = _num(a.get("r"), 1.0)
        if r <= 0:
            return None
        self.rep.exact_parts += 1
        if self._faceted(a):
            nf = _fragments(r, a)
            rings = (nf + 1) // 2
            P, F = [], []
            for i in range(rings):
                phi = math.pi * (i + 0.5) / rings
                for j in range(nf):
                    t = 2 * math.pi * j / nf
                    P.append((r * math.sin(phi) * math.cos(t), r * math.sin(phi) * math.sin(t), r * math.cos(phi)))
            F.append(list(range(nf)))
            F.append(list(range((rings - 1) * nf, rings * nf))[::-1])
            for i in range(rings - 1):
                for j in range(nf):
                    a0, a1 = i * nf + j, i * nf + (j + 1) % nf
                    F.append([a0, a1, a1 + nf, a0 + nf])
            return polyhedron_solid(_xf(M, np.array(P)), F)
        self.rep.round_parts += 1
        return _apply(BRepPrimAPI_MakeSphere(r).Shape(), M)

    def cylinder(self, a, M):
        h = _num(a.get("h"), 1.0)
        r1, r2 = _num(a.get("r1"), 1.0), _num(a.get("r2"), 1.0)
        if h <= 0 or (r1 <= 0 and r2 <= 0):
            return None
        z0 = -h / 2 if a.get("center") else 0.0
        self.rep.exact_parts += 1
        if self._faceted(a):
            nf = _fragments(max(r1, r2), a)
            t = 2 * math.pi * np.arange(nf) / nf
            ring = np.c_[np.cos(t), np.sin(t)]
            P, F = [], []
            if r1 > 0:
                P += [(x * r1, y * r1, z0) for x, y in ring]
            else:
                P.append((0, 0, z0))
            b = len(P)
            if r2 > 0:
                P += [(x * r2, y * r2, z0 + h) for x, y in ring]
            else:
                P.append((0, 0, z0 + h))
            for j in range(nf):
                k = (j + 1) % nf
                if r1 > 0 and r2 > 0:
                    F.append([j, k, b + k, b + j])
                elif r1 > 0:
                    F.append([j, k, b])
                else:
                    F.append([0, b + k, b + j])
            if r1 > 0:
                F.append(list(range(nf))[::-1])
            if r2 > 0:
                F.append(list(range(b, b + nf)))
            return polyhedron_solid(_xf(M, np.array(P, float)), F)
        self.rep.round_parts += 1
        ax = gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1))
        if abs(r1 - r2) < 1e-12:
            s = BRepPrimAPI_MakeCylinder(ax, r1, h).Shape()
        else:
            s = BRepPrimAPI_MakeCone(ax, r1, r2, h).Shape()
        return _apply(s, M)

    def linear_extrude(self, n):
        a = n.args
        h = _num(a.get("height"), 100.0)
        if h <= 0:
            return None
        reg = self.union2(self.kids(n, 2), I4)
        if _is_empty(reg):
            return None
        twist = _num(a.get("twist"))
        sc = _vec(a.get("scale", 1), 2, 1.0)
        z0 = -h / 2 if a.get("center") else 0.0
        if a.get("v") is not None:
            v = np.array(_vec(a.get("v"), 3, 0.0))
            if np.linalg.norm(v) > 0 and not np.allclose(v / np.linalg.norm(v), [0, 0, 1]):
                raise _NeedMesh("slanted linear_extrude")
        solids = []
        simple = abs(twist) < 1e-9 and abs(sc[0] - 1) < 1e-12 and abs(sc[1] - 1) < 1e-12
        for f in _faces(reg):
            if simple:
                solids.append(BRepPrimAPI_MakePrism(f, gp_Vec(0, 0, h)).Shape())
                continue
            outer = _S(BRepTools, "OuterWire")(f)
            inner = [_cast("Wire", w) for w in _explore(f, TopAbs_WIRE) if not _cast("Wire", w).IsSame(outer)]
            body = self._loft(outer, h, twist, sc)
            holes = [self._loft(w, h, twist, sc) for w in inner]
            solids.append(self.boolean("cut", [body], holes) if holes else body)
        self.rep.exact_parts += 1
        res = self.boolean("fuse", solids)
        if res is not None and z0:
            tr = gp_Trsf()
            tr.SetTranslation(gp_Vec(0, 0, z0))
            res = BRepBuilderAPI_Transform(res, tr, True).Shape()
        return res

    def _loft(self, wire, h, twist, sc):
        n = 1 if abs(twist) < 1e-9 else max(3, int(math.ceil(abs(twist) / 12.0)))
        ts = BRepOffsetAPI_ThruSections(True, n == 1, 1e-6)
        for k in range(n + 1):
            t = k / n
            sx, sy = 1 + (sc[0] - 1) * t, 1 + (sc[1] - 1) * t
            ang = -math.radians(twist) * t
            if k == n and (sx <= 1e-12 or sy <= 1e-12):
                if sx > 1e-12 or sy > 1e-12:
                    raise _NeedMesh("scale to a line")
                from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
                ts.AddVertex(BRepBuilderAPI_MakeVertex(gp_Pnt(0, 0, h)).Vertex())
                continue
            c, s = math.cos(ang), math.sin(ang)
            Mk = np.array([[c * sx, -s * sy, 0, 0], [s * sx, c * sy, 0, 0], [0, 0, 1, h * t], [0, 0, 0, 1]])
            ts.AddWire(_cast("Wire", _apply(wire, Mk)) if not np.allclose(Mk, I4) else wire)
        ts.CheckCompatibility(False)
        ts.Build()
        if not ts.IsDone():
            raise _NeedMesh("loft failed")
        return ts.Shape()

    def rotate_extrude(self, n):
        a = n.args
        ang = _num(a.get("angle"), 360.0)
        if abs(ang) < 1e-9:
            return None
        ang = max(-360.0, min(360.0, ang))
        if self._faceted(a):
            raise _NeedMesh(f"$fn={_num(a.get('$fn')):g} asks for a faceted revolution")
        reg = self.union2(self.kids(n, 2), I4)
        if _is_empty(reg):
            return None
        xs = [p for f in _faces(reg) for p in _wire_pts(_S(BRepTools, "OuterWire")(f), 4)[:, 0]]
        if xs and min(xs) < -1e-9 and max(xs) > 1e-9:
            raise ScadError("rotate_extrude: the 2D shape crosses the Y axis (OpenSCAD refuses this too)")
        to_xz = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float)
        prof = _apply(reg, to_xz)
        solids = []
        for f in _faces(prof):
            solids.append(BRepPrimAPI_MakeRevol(f, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
                                                math.radians(ang), True).Shape())
        self.rep.exact_parts += 1
        self.rep.round_parts += 1
        res = self.boolean("fuse", solids)
        st = _num(a.get("start"), 0.0)
        if res is not None and st:
            tr = gp_Trsf()
            tr.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(st))
            res = BRepBuilderAPI_Transform(res, tr, True).Shape()
        return res

    def _sphere_of(self, n, M=I4):
        """(center, radius) if this subtree is just a (transformed) round sphere."""
        while n.name in _PASS or n.name == "multmatrix":
            ks = [c for c in n.children if self.dim(c)]
            if len(ks) != 1:
                return None
            if n.name == "multmatrix":
                M = M @ np.array(n.args.get("m", n.args.get(0)), float)
            n = ks[0]
        if n.name != "sphere" or self._faceted(n.args):
            return None
        sim = _similarity(M)
        if sim is None:
            return None
        return M[:3, 3].copy(), _num(n.args.get("r"), 1.0) * sim[0]

    def _circle_of(self, n, M=I4):
        while n.name in _PASS or n.name == "multmatrix":
            ks = [c for c in n.children if self.dim(c)]
            if len(ks) != 1:
                return None
            if n.name == "multmatrix":
                M = M @ np.array(n.args.get("m", n.args.get(0)), float)
            n = ks[0]
        if n.name != "circle" or self._faceted(n.args):
            return None
        sim = _similarity(M, 2)
        if sim is None:
            return None
        return M[:2, 3].copy(), _num(n.args.get("r"), 1.0) * sim[0]

    def minkowski3(self, n, M):
        ks = self.kids(n, 3)
        if len(ks) == 1:
            return self.solid(ks[0], M)
        if len(ks) == 2:
            for i in (0, 1):
                sp = self._sphere_of(ks[i])
                if sp is None:
                    continue
                c, r = sp
                base = self.solid(ks[1 - i], I4)
                if base is None:
                    return None
                mk = BRepOffsetAPI_MakeOffsetShape()
                mk.PerformByJoin(base, r, 1e-6, _SKIN, False, False, GeomAbs_Arc)
                if not mk.IsDone():
                    raise _NeedMesh("offset failed")
                res = _as_solid(mk.Shape())
                if res is None:
                    raise _NeedMesh("offset gave no solid")
                if not BRepCheck_Analyzer(res).IsValid() or abs(_volume(res)) <= abs(_volume(base)):
                    raise _NeedMesh("offset gave bad geometry")
                T = np.eye(4)
                T[:3, 3] = c
                self.rep.round_parts += 1
                return _apply(res, M @ T)
        raise _NeedMesh("minkowski")

    def resize(self, n, M, d):
        a = n.args
        new = _vec(a.get("newsize", 0), 3, 0.0)[:d]
        auto = a.get("auto", False)
        auto = _vec([1.0 if x else 0.0 for x in auto] if isinstance(auto, list) else (1.0 if auto else 0.0), 3, 0.0)
        child = self.union3(self.kids(n, 3), I4) if d == 3 else self.union2(self.kids(n, 2), I4)
        if child is None:
            return None
        lo, hi = _bbox(child)
        size = (hi - lo)[:d]
        f = [new[i] / size[i] if new[i] > 0 and size[i] > 1e-12 else 0.0 for i in range(d)]
        mx = max(f) if max(f) > 0 else 1.0
        f = [f[i] if f[i] > 0 else (mx if auto[i] else 1.0) for i in range(d)]
        S = np.eye(4)
        for i in range(d):
            S[i, i] = f[i]
        return _apply(child, M @ S)

    # ------------------------------------------------------------ 2D (faces in the XY plane)
    def region(self, n, M):
        try:
            return self._region(n, M)
        except _NeedMesh as e:
            why = str(e)
        except ScadError:
            raise
        except Exception as e:  # noqa
            why = f"{e.__class__.__name__}: {e}"
        self.log(f"  2D '{n.name}' rendered by OpenSCAD ({why})")
        return self.render_2d(n, M)

    def union2(self, kids, M):
        return self.boolean("fuse", [self.region(k, M) for k in kids])

    def _region(self, n, M):
        a, nm = n.args, n.name
        if "%" in n.mod or "*" in n.mod:
            return None
        if nm == "multmatrix":
            return self.union2(self.kids(n, 2), M @ np.array(a.get("m", a.get(0)), float))
        if nm in _PASS:
            return self.union2(self.kids(n, 2), M)
        if nm == "difference":
            ks = self.kids(n, 2)
            if not ks:
                return None
            base = self.region(ks[0], M)
            return self.boolean("cut", [base], [self.region(k, M) for k in ks[1:]]) if base else None
        if nm == "intersection":
            shapes = [self.region(k, M) for k in self.kids(n, 2)]
            if not shapes or any(s is None for s in shapes):
                return None
            res = shapes[0]
            for s in shapes[1:]:
                res = self.boolean("common", [res], [s])
                if res is None:
                    return None
            return res
        if nm == "square":
            s = _vec(a.get("size", 1), 2, 1.0)
            if min(s) <= 0:
                return None
            c = np.array(s) / 2 if a.get("center") else np.zeros(2)
            P = np.array([[0, 0], [s[0], 0], [s[0], s[1]], [0, s[1]]]) - c
            return _faces_from_loops([_xf(M, P)[:, :2]])
        if nm == "circle":
            r = _num(a.get("r"), 1.0)
            if r <= 0:
                return None
            if self._faceted(a):
                nf = _fragments(r, a)
                t = 2 * math.pi * np.arange(nf) / nf
                return _faces_from_loops([_xf(M, np.c_[r * np.cos(t), r * np.sin(t)])[:, :2]])
            self.rep.round_parts += 1
            c = _xf(M, np.zeros((1, 2)))[0]
            L = M[:2, :2]
            U, S, Vt = np.linalg.svd(L)
            if S.min() < 1e-12:
                return None
            if S.max() / S.min() - 1 < 2e-4:
                e = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(_gp(c), gp_Dir(0, 0, 1)), r * float(np.sqrt(S[0] * S[1])))).Edge()
            else:
                ax = gp_Ax2(_gp(c), gp_Dir(0, 0, 1), gp_Dir(float(U[0, 0]), float(U[1, 0]), 0))
                e = BRepBuilderAPI_MakeEdge(gp_Elips(ax, r * float(S[0]), r * float(S[1]))).Edge()
            return _faces_from_wires([BRepBuilderAPI_MakeWire(e).Wire()])
        if nm == "polygon":
            pts = np.array(a.get("points", []), float)
            if len(pts) < 3:
                return None
            W = _xf(M, pts[:, :2])[:, :2]
            paths = a.get("paths") or [list(range(len(pts)))]
            return _faces_from_loops([W[[int(i) for i in p]] for p in paths if len(p) >= 3])
        if nm == "offset":
            return _apply(self.offset(n), M)
        if nm == "hull":
            reg = self.union2(self.kids(n, 2), M)
            return hull2d(reg) if reg is not None else None
        if nm == "minkowski":
            ks = self.kids(n, 2)
            if len(ks) == 1:
                return self.region(ks[0], M)
            if len(ks) == 2:
                for i in (0, 1):
                    ci = self._circle_of(ks[i])
                    if ci is None:
                        continue
                    base = self.region(ks[1 - i], I4)
                    res = self._offset_faces(base, ci[1], True)
                    T = np.eye(4)
                    T[:2, 3] = ci[0]
                    return _apply(res, M @ T)
            raise _NeedMesh("minkowski")
        if nm == "resize":
            return self.resize(n, M, 2)
        raise _NeedMesh(nm)   # text, projection, import(svg/dxf), fill, unknown

    def offset(self, n):
        a = n.args
        base = self.union2(self.kids(n, 2), I4)
        if base is None:
            return None
        if a.get("r") is not None and _num(a.get("r")) != 0 and not (isinstance(a.get("r"), float) and math.isnan(a["r"])):
            return self._offset_faces(base, _num(a.get("r")), True)
        d = _num(a.get("delta"))
        if d == 0:
            return base
        if a.get("chamfer"):
            raise _NeedMesh("offset chamfer")
        return self._offset_faces(base, d, False)

    def _offset_faces(self, base, dist, rounded):
        if base is None:
            return None
        if abs(dist) < 1e-12:
            return base
        out = []
        for f in _faces(base):
            mk = BRepOffsetAPI_MakeOffset(f, GeomAbs_Arc if rounded else GeomAbs_Intersection, False)
            mk.Perform(float(dist), 0.0)
            if not mk.IsDone():
                raise _NeedMesh("2D offset failed")
            wires = [_cast("Wire", w) for w in _explore(mk.Shape(), TopAbs_WIRE)]
            fc = _faces_from_wires(wires)
            if fc is not None:
                out.append(fc)
        if dist > 0:
            return self.boolean("fuse", out)
        return _compound(out)

    # ------------------------------------------------------------ OpenSCAD renders a part
    def resolve(self, f):
        f = str(f or "")
        if not f:
            return f
        if os.path.isabs(f):
            return f
        p = os.path.join(self.src_dir or "", f)
        return os.path.abspath(p)

    def _subtree_scad(self, n, fn):
        t = self.text[n.start:n.end]

        def fix_file(m):
            p = self.resolve(m.group(2))
            if self.src_dir is None and not os.path.isabs(m.group(2)):
                raise ScadError(f"'{m.group(2)}' is referenced by a relative path — convert the .scad "
                                "from its folder (not an uploaded .csg) so the file can be found")
            return m.group(1) + '"' + p.replace("\\", "/") + '"'
        t = re.sub(r'(\bfile\s*=\s*)"((?:[^"\\]|\\.)*)"', fix_file, t)

        def bump(m):
            v = float(m.group(1))
            if v == 0 or v > self.prm.faceted_fn:
                v = max(v, fn)
            return f"$fn = {v:g}"
        t = re.sub(r"\$fn\s*=\s*([-+0-9.eE]+)", bump, t)
        if n.end < len(self.text) and not t.rstrip().endswith((";", "}")):
            t += ";"
        self.n_sub += 1
        p = os.path.join(self.tmp, f"part{self.n_sub}.scad")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(t.lstrip("#!") + "\n")
        return p

    def _need_oscad(self, what):
        if self.oscad is None:
            raise ScadError(f"'{what}' needs OpenSCAD to render it, but OpenSCAD wasn't found. "
                            "Install OpenSCAD or set its location.")

    def render_mesh(self, n, M):
        self._need_oscad(n.name)
        fn = 32 if n.name == "minkowski" else self.prm.fallback_fn
        src = self._subtree_scad(n, fn)
        out = src[:-5] + ".off"
        self.log(f"  OpenSCAD is rendering '{n.name}' (taken over as exact facets)…")
        self.oscad.run(src, out, quiet=True)
        V, F = read_off(out)
        if not F:
            return None
        self.rep.mesh_parts += 1
        self.log(f"    {len(F)} facets")
        sol = polyhedron_solid(_xf(M, V), F, self.log)
        if sol is None:
            raise ScadError(f"'{n.name}' could not be turned into a closed solid")
        return sol

    def render_2d(self, n, M):
        self._need_oscad(n.name)
        src = self._subtree_scad(n, self.prm.fallback_fn)
        out = src[:-5] + ".svg"
        self.oscad.run(src, out, quiet=True)
        loops = []
        svg = open(out, encoding="utf-8").read()
        for d in re.findall(r'\bd="([^"]*)"', svg):
            for sub in re.split(r"[Mm]", d):
                nums = [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", sub)]
                if len(nums) >= 6:
                    P = np.array(nums[: len(nums) // 2 * 2]).reshape(-1, 2)
                    P[:, 1] *= -1
                    loops.append(_xf(M, P)[:, :2])
        self.rep.outline_parts += 1
        wires, pts = [], []
        for P in loops:
            P = _clean_loop(P, 1e-9)
            if len(P) < 3 or abs(_sarea(P)) < 1e-12:
                continue
            w = _smooth_wire(P) if self.prm.smooth_outlines else None
            w = w or _wire_from_pts(P)
            if w is not None:
                wires.append(w)
                pts.append(P)
        return _faces_from_wires(wires, pts)

    # ------------------------------------------------------------ exact hulls
    def _hull_leaves(self, kids, M, out):
        for k in kids:
            if k.name in _PASS or k.name == "union":
                self._hull_leaves(self.kids(k, 3), M, out)
            elif k.name == "multmatrix":
                self._hull_leaves(self.kids(k, 3), M @ np.array(k.args.get("m", k.args.get(0)), float), out)
            elif k.name == "hull":                 # hull of hulls = one hull
                self._hull_leaves(self.kids(k, 3), M, out)
            else:
                out.append((k, M))
        return out

    def hull3(self, n, M):
        leaves = self._hull_leaves(self.kids(n, 3), M, [])
        if not leaves:
            return None
        # 1) equal spheres (rounded boxes, capsules, pills)
        sph = [self._sphere_of(k, Mk) for k, Mk in leaves]
        if all(sph):
            rs = np.array([r for _, r in sph])
            if rs.max() - rs.min() <= 1e-9 * max(1.0, rs.max()):
                res = self._sphere_hull(np.array([c for c, _ in sph]), float(rs.mean()))
                if res is not None:
                    self.rep.exact_parts += 1
                    self.rep.round_parts += 1
                    return res
        shapes = [self.solid(k, Mk) for k, Mk in leaves]
        shapes = [s for s in shapes if not _is_empty(s)]
        if not shapes:
            return None
        whole = _compound(shapes)
        kinds = [BRepAdaptor_Surface(f).GetType() for f in _faces(whole)]
        # 2) everything flat-faced: exact convex polyhedron
        if all(k == GeomAbs_Plane for k in kinds):
            pts = [_S(BRep_Tool, "Pnt")(_cast("Vertex", v)) for v in _explore(whole, TopAbs_VERTEX)]
            res = _convex_hull_solid([(p.X(), p.Y(), p.Z()) for p in pts])
            if res is not None:
                return res
        # 3) straight prisms / cylinders along one axis with the same length (rounded plates)
        res = self._prism_hull(whole)
        if res is not None:
            self.rep.round_parts += 1
            return res
        raise _NeedMesh("hull of mixed shapes")

    def _prism_hull(self, whole):
        axes = []
        for f in _faces(whole):
            ad = BRepAdaptor_Surface(f)
            if ad.GetType() == GeomAbs_Cylinder:
                d = ad.Cylinder().Axis().Direction()
                a = np.array([d.X(), d.Y(), d.Z()])
                if not any(abs(abs(a @ b) - 1) < 1e-9 for b in axes):
                    axes.append(a)
        for a in axes:
            R, Rinv = _frame_to_z(a)
            loc = _apply(whole, R)
            lo, hi = _bbox(loc)
            caps, ok = [], True
            for sol in _solids(loc):
                slo, shi = _bbox(sol)
                if abs(slo[2] - lo[2]) > 1e-4 or abs(shi[2] - hi[2]) > 1e-4:
                    ok = False
                    break
                for f in _faces(sol):
                    ad = BRepAdaptor_Surface(f)
                    t = ad.GetType()
                    if t == GeomAbs_Plane:
                        d = ad.Plane().Axis().Direction()
                        nz = abs(d.Z())
                        if nz > 1 - 1e-9:
                            flo, fhi = _bbox(f)
                            if abs(flo[2] - lo[2]) < 1e-4 and abs(fhi[2] - lo[2]) < 1e-4:
                                caps.append(f)
                            elif not (abs(flo[2] - hi[2]) < 1e-4 and abs(fhi[2] - hi[2]) < 1e-4):
                                ok = False
                        elif nz > 1e-9:
                            ok = False
                    elif t == GeomAbs_Cylinder:
                        d = ad.Cylinder().Axis().Direction()
                        if abs(d.Z()) < 1 - 1e-9:
                            ok = False
                    else:
                        ok = False
                    if not ok:
                        break
                if not ok:
                    break
            if not ok or not caps:
                continue
            T = np.eye(4)
            T[2, 3] = -lo[2]
            base = hull2d(_apply(_compound(caps), T))
            if base is None:
                continue
            h = float(hi[2] - lo[2])
            prism = _compound([BRepPrimAPI_MakePrism(f, gp_Vec(0, 0, h)).Shape() for f in _faces(base)])
            Tb = np.eye(4)
            Tb[2, 3] = lo[2]
            return _apply(prism, Rinv @ Tb)
        return None

    def _sphere_hull(self, C, r):
        """Exact hull of equal spheres = (hull of centres) grown by r."""
        C = np.unique(np.round(C, 9), axis=0)
        if len(C) == 1:
            T = np.eye(4)
            T[:3, 3] = C[0]
            return _apply(BRepPrimAPI_MakeSphere(r).Shape(), T)
        c0 = C.mean(0)
        _, S, Vt = np.linalg.svd(C - c0)
        rank = int(np.sum(S > 1e-9 * max(1.0, S[0])))
        ball = lambda p: _apply(BRepPrimAPI_MakeSphere(r).Shape(), _translate(p))

        def rod(p, q):
            d = np.asarray(q) - np.asarray(p)
            L = float(np.linalg.norm(d))
            return BRepPrimAPI_MakeCylinder(gp_Ax2(_gp(p), gp_Dir(*map(float, d / L))), r, L).Shape()
        if rank == 1:
            t = (C - c0) @ Vt[0]
            p, q = C[np.argmin(t)], C[np.argmax(t)]
            return self.boolean("fuse", [rod(p, q), ball(p), ball(q)])
        if rank == 2:
            nrm = Vt[2]
            R, Rinv = _frame_to_z(nrm)
            loc = _xf(R, C)
            z = float(loc[:, 2].mean())
            hi = _chain_hull(loc[:, :2])
            poly = loc[hi]
            ring = [_xf(Rinv, [[x, y, z]])[0] for x, y, _ in poly]
            face = _faces_from_loops([poly[:, :2]])
            slab = BRepPrimAPI_MakePrism(_faces(face)[0], gp_Vec(0, 0, 2 * r)).Shape()
            slab = _apply(slab, Rinv @ _translate([0, 0, z - r]))
            parts = [slab] + [rod(ring[k], ring[(k + 1) % len(ring)]) for k in range(len(ring))] + [ball(p) for p in ring]
            return self.boolean("fuse", parts)
        # full 3D: offset the convex hull of the centres with rounded (arc) joins
        core_ = _convex_hull_solid(C)
        if core_ is None:
            return None
        try:
            mk = BRepOffsetAPI_MakeOffsetShape()
            mk.PerformByJoin(core_, r, 1e-7, _SKIN, False, False, GeomAbs_Arc)
            if mk.IsDone():
                res = _as_solid(mk.Shape())
                if res is not None and BRepCheck_Analyzer(res).IsValid() and abs(_volume(res)) > abs(_volume(core_)):
                    return res
        except Exception:
            pass
        from scipy.spatial import ConvexHull
        h = ConvexHull(C)
        parts = [core_]
        for simp, eq in zip(h.simplices, h.equations):
            P = C[simp]
            parts.append(polyhedron_solid(np.vstack([P, P + r * eq[:3]]),
                                          [[0, 1, 2], [3, 5, 4], [0, 3, 4, 1], [1, 4, 5, 2], [2, 5, 3, 0]]))
        ed = {tuple(sorted((int(a), int(b)))) for s_ in h.simplices for a, b in ((s_[0], s_[1]), (s_[1], s_[2]), (s_[2], s_[0]))}
        parts += [rod(C[a], C[b]) for a, b in ed] + [ball(C[i]) for i in h.vertices]
        return self.boolean("fuse", parts)


def _translate(p):
    T = np.eye(4)
    T[:3, 3] = np.asarray(p, float)[:3]
    return T


def _as_solid(shape):
    sol = _solids(shape)
    if sol:
        return sol[0] if len(sol) == 1 else _compound(sol)
    shells = [_cast("Shell", s) for s in _explore(shape, TopAbs_SHELL)]
    if len(shells) != 1:
        return None
    ms = BRepBuilderAPI_MakeSolid(shells[0])
    if not ms.IsDone():
        return None
    so = ms.Solid()
    try:
        _S(BRepLib, "OrientClosedSolid")(so)
    except Exception:
        pass
    return so


# ====================================================================== customizer
def scan_parameters(path):
    """Customizer-style parameters: top-level `name = literal;` before the first module/function."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    out, group, depth = [], "", 0
    lines = text.splitlines()
    in_block_comment = False
    for li, line in enumerate(lines):
        s = line.strip()
        if in_block_comment:
            if "*/" in s:
                in_block_comment = False
            continue
        g = re.match(r"/\*\s*\[(.+?)\]\s*\*/", s)
        if g and depth == 0:
            group = g.group(1).strip()
            continue
        if s.startswith("/*") and "*/" not in s:
            in_block_comment = True
            continue
        if depth == 0 and re.match(r"(module|function)\b", s):
            break
        if depth == 0:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?);\s*(?://\s*(.*))?$", s)
            if m and group.lower() != "hidden":
                name, lit, comment = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
                kind = None
                if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", lit):
                    kind = "number"
                elif lit in ("true", "false"):
                    kind = "bool"
                elif re.fullmatch(r'"(?:[^"\\]|\\.)*"', lit):
                    kind = "string"
                elif re.fullmatch(r"\[\s*[-+0-9.eE\s,]*\]", lit):
                    kind = "vector"
                if kind:
                    p = {"name": name, "value": lit, "kind": kind, "group": group, "desc": "", "options": None}
                    # description is the comment line just above
                    if li > 0 and lines[li - 1].strip().startswith("//"):
                        p["desc"] = lines[li - 1].strip()[2:].strip()
                    rng = re.fullmatch(r"\[\s*([-+0-9.eE]+)\s*:\s*([-+0-9.eE]+)\s*(?::\s*([-+0-9.eE]+))?\s*\]", comment)
                    if rng:
                        lo, x, y = rng.groups()
                        if y is None:
                            p.update(min=float(lo), max=float(x), step=None)
                        else:
                            p.update(min=float(lo), step=float(x), max=float(y))
                    elif re.fullmatch(r"\[.*\]", comment):
                        opts = [o.strip() for o in comment[1:-1].split(",") if o.strip()]
                        p["options"] = [o.split(":", 1)[0].strip() for o in opts]
                        p["labels"] = [o.split(":", 1)[-1].strip() for o in opts]
                    elif comment and not p["desc"]:
                        p["desc"] = comment
                    out.append(p)
        depth += line.count("{") - line.count("}")
    return out


def parse_deps(path):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    txt = txt.replace("\\\n", " ")
    _, _, rest = txt.partition(":")
    parts = re.split(r"(?<!\\)\s+", rest.strip())
    return [p.replace("\\ ", " ") for p in parts if p]


# ====================================================================== main entry
def convert(in_path, out_path, prm: ScadParams | None = None, log=None, progress=None) -> ScadReport:
    prm = prm or ScadParams()
    L = _Log(log, progress)
    rep = ScadReport(out_path=str(out_path))
    t0 = time.time()
    in_path = os.path.abspath(in_path)
    ext = os.path.splitext(in_path)[1].lower()
    cmd = find_openscad(prm.openscad)
    oscad = OpenSCAD(cmd, prm.timeout, L) if cmd else None
    tmp = tempfile.mkdtemp(prefix="scad2step_")
    try:
        if ext == ".csg":
            csg = in_path
            src_dir = os.path.dirname(in_path)
            if not cmd:
                L("OpenSCAD not found — converting the CSG without it (hull/text/imports won't work)")
        else:
            if not cmd:
                raise ScadError("OpenSCAD wasn't found. Install it from openscad.org, or tell this app "
                                "where it is (Settings / --openscad PATH). Or export File → Export → CSG "
                                "in OpenSCAD and convert that .csg instead.")
            L(f"OpenSCAD: {' '.join(cmd)}")
            csg = os.path.join(tmp, "model.csg")
            L("OpenSCAD is evaluating the model…")
            oscad.run(in_path, csg, prm.defines, deps=os.path.join(tmp, "deps.d"))
            src_dir = os.path.dirname(in_path)
            rep.deps = [os.path.abspath(os.path.join(src_dir, d)) for d in parse_deps(os.path.join(tmp, "deps.d"))]
        L.p(0.1)
        text = open(csg, encoding="utf-8", errors="replace").read()
        roots = parse_csg(text)
        b = Builder(text, prm, L, src_dir, oscad, tmp)
        b.rep = rep
        n_nodes = sum(1 for _ in _walk(roots))
        L(f"CSG tree: {n_nodes} nodes. Building exact geometry…")
        tops = [r for r in roots if b.dim(r) == 3]
        if not tops:
            if any(b.dim(r) == 2 for r in roots):
                raise ScadError("This model is 2D only. STEP needs a 3D solid — wrap it in linear_extrude().")
            raise ScadError("The model is empty (nothing to convert)")
        shapes = []
        for k, r in enumerate(tops):
            shapes.append(b.solid(r, I4))
            L.p(0.1 + 0.6 * (k + 1) / len(tops))
        shape = b.boolean("fuse", shapes)
        if shape is None:
            raise ScadError("The model's result is empty")
        if prm.scale != 1.0:
            tr = gp_Trsf()
            tr.SetScale(gp_Pnt(0, 0, 0), prm.scale)
            shape = BRepBuilderAPI_Transform(shape, tr, True).Shape()
        L("Merging faces and checking the solid…")
        shape = _safe_unify(shape, max(1e-6, prm.fuzzy), L)
        sol = _solids(shape)
        if len(sol) > 1:
            shape = _compound(sol)
        L.p(0.8)
        L("Writing STEP…")
        write_step(shape, out_path)
        back = read_step(out_path)
        rep.n_solids = len(_solids(back))
        rep.is_solid = rep.n_solids > 0 and _sealed(back)
        rep.is_valid = BRepCheck_Analyzer(back).IsValid()
        rep.n_faces = _count_faces(back)
        if rep.n_solids:
            rep.step_volume = abs(_volume(back))
        L.p(0.85)
        if prm.verify and oscad is not None:
            try:
                L("Comparing with OpenSCAD's own render…")
                off = os.path.join(tmp, "check.off")
                oscad.run(in_path, off, prm.defines if ext != ".csg" else None, quiet=True)
                V, F = read_off(off)
                V = V * prm.scale
                rep.scad_volume = abs(mesh_volume(V, F))
                lo, hi = _bbox(back)
                rep.size_diff = float(max(np.abs(lo - V.min(0)).max(), np.abs(hi - V.max(0)).max()))
            except Exception as e:  # noqa
                L(f"  (comparison skipped: {e})")
        rep.seconds = time.time() - t0
        rep.ok = True
        L.p(1.0)
        L(rep.summary())
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _walk(nodes):
    for n in nodes:
        yield n
        yield from _walk(n.children)


def watch(in_path, out_path, prm: ScadParams, log=print, stop=None, interval=1.0, on_done=None, lock=None):
    """Re-convert every time the .scad (or any file it uses) is saved. Runs until stop() is true."""
    stop = stop or (lambda: False)
    files = [os.path.abspath(in_path)]
    last = None
    log(f"Watching {in_path} — save it in OpenSCAD and the STEP updates. (Ctrl+C to stop)")
    while not stop():
        try:
            stamp = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in files)
        except OSError:
            stamp = None
        if stamp != last:
            last = stamp
            time.sleep(0.3)                       # let the editor finish writing
            log(f"\n=== {time.strftime('%H:%M:%S')}  converting {os.path.basename(in_path)}")
            try:
                import contextlib
                with (lock or contextlib.nullcontext()):
                    rep = convert(in_path, out_path, prm, log=log)
                files = sorted({os.path.abspath(in_path), *rep.deps})
                last = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in files)
                log(f"Saved {out_path}")
                if on_done:
                    on_done(rep, None)
            except Exception as e:  # noqa
                log(f"ERROR: {e}")
                if on_done:
                    on_done(None, e)
        time.sleep(interval)
