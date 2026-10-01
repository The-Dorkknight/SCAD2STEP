"""
Browser interface for SCAD2STEP — no Tk or other GUI toolkit needed.

Runs a small web server on 127.0.0.1 (only this computer can reach it) and opens the
page in the default browser. A .scad can be picked from disk (so its libraries are found
and the live link can watch it) or dropped in; results are saved next to the .scad or in
the Output folder and offered as a download.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

import scad2step_core as core

UNITS = {"mm": 1.0, "inches": 25.4, "cm": 10.0, "m": 1000.0}
SCAD_EXT = {".scad", ".csg"}


def _safe_name(name, default="file"):
    name = os.path.basename(unquote(name or "")).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name or default


def _unique(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def _lower_priority():
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(5)
    except Exception:
        pass


def native_open_dialog(start=""):
    """Ask the OS for a file path with its own file dialog. None if cancelled/unavailable."""
    start = start or os.path.expanduser("~")
    try:
        if sys.platform == "darwin":
            scr = ('POSIX path of (choose file with prompt "Choose an OpenSCAD model" '
                   'of type {"scad", "csg", "public.item"})')
            r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
        if sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                  "$d.Filter='OpenSCAD (*.scad;*.csg)|*.scad;*.csg|All files (*.*)|*.*';"
                  "$d.Title='Choose an OpenSCAD model';"
                  "$f=New-Object System.Windows.Forms.Form;$f.TopMost=$true;"
                  "if($d.ShowDialog($f) -eq 'OK'){Write-Output $d.FileName}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True,
                               text=True, timeout=600, creationflags=0x08000000)
            return r.stdout.strip() or None
        if shutil.which("zenity"):
            r = subprocess.run(["zenity", "--file-selection", "--title=Choose an OpenSCAD model",
                                "--file-filter=OpenSCAD | *.scad *.csg", "--file-filter=All files | *",
                                f"--filename={start.rstrip('/')}/"], capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
        if shutil.which("kdialog"):
            r = subprocess.run(["kdialog", "--getopenfilename", start, "*.scad *.csg|OpenSCAD models"],
                               capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
    except Exception:
        return None
    return None


def _step_to_obj(step_path, obj_path):
    """Convert STEP file to OBJ for 3D preview in browser."""
    try:
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
    except ImportError:
        raise ImportError("OCP (Open Cascade Python) not available for preview generation")

    try:
        reader = STEPControl_Reader()
        status = reader.ReadFile(step_path)
        if status != 0:
            raise RuntimeError(f"Failed to read STEP file (status {status})")
        reader.TransferRoots()
        shape = reader.OneShape()
    except Exception as e:
        raise RuntimeError(f"Failed to load STEP: {e}")

    # Tessellate the shape for OBJ export
    try:
        BRepMesh_IncrementalMesh(shape, 0.1)
    except Exception:
        pass  # Meshing may fail for some shapes, continue with what we have

    vertices = []
    faces = []
    vertex_map = {}

    try:
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = explorer.Current()
            try:
                from OCP.BRep import BRep_Tool
                tri = BRep_Tool.Triangulation(face)
                if tri:
                    offset = len(vertices)
                    nodes = tri.Nodes()
                    for i in range(1, nodes.Length() + 1):
                        pt = nodes.Value(i)
                        vertices.append((float(pt.X()), float(pt.Y()), float(pt.Z())))

                    triangles = tri.Triangles()
                    for i in range(1, triangles.NbTriangles() + 1):
                        tri_obj = triangles.Triangle(i)
                        n1, n2, n3 = tri_obj.Value(1) - 1, tri_obj.Value(2) - 1, tri_obj.Value(3) - 1
                        faces.append((offset + n1, offset + n2, offset + n3))
            except Exception:
                pass
            explorer.Next()
    except Exception as e:
        raise RuntimeError(f"Failed to extract faces: {e}")

    if not vertices:
        raise RuntimeError("No geometry found in STEP file")

    with open(obj_path, "w") as f:
        for x, y, z in vertices:
            f.write(f"v {float(x)} {float(y)} {float(z)}\n")
        for v1, v2, v3 in faces:
            f.write(f"f {v1 + 1} {v2 + 1} {v3 + 1}\n")


def _prm_from(p, defines):
    return core.ScadParams(
        defines=defines,
        scale=UNITS.get(p.get("units", "mm"), 1.0),
        faceted_fn=int(float(p.get("faceted_fn") or 8)),
        verify=bool(p.get("verify", True)),
        smooth_outlines=bool(p.get("smooth_outlines", True)),
        openscad=core.load_settings().get("openscad", ""))


def _defines_from(values, params):
    """Only pass values that differ from the file's own defaults."""
    out = {}
    defaults = {q["name"]: q for q in params}
    for k, v in (values or {}).items():
        q = defaults.get(k)
        if q is None:
            continue
        v = str(v).strip()
        if q["kind"] == "string":
            v = json.dumps(v)
        elif q["kind"] == "bool":
            v = "true" if v in ("true", "True", "1", "on") else "false"
        elif q["kind"] == "number":
            try:
                float(v)
            except ValueError:
                continue
        if v != q["value"]:
            out[k] = v
    return out


class App:
    def __init__(self, work_dir, out_dir):
        self.work, self.out = work_dir, out_dir
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.out, exist_ok=True)
        self.token = secrets.token_urlsafe(16)
        self.files = {}      # id -> {"path", "name", "local": bool}
        self.jobs = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.watcher = None  # {"file", "stop": Event, "log": [], "runs": n, "last": {...}}
        self.oscad_info = None
        self.preselect = None

    # ---------------------------------------------------------------- OpenSCAD
    def openscad_status(self, refresh=False):
        if self.oscad_info is None or refresh:
            cmd = core.find_openscad()
            info = {"found": bool(cmd), "cmd": " ".join(cmd) if cmd else "", "version": ""}
            if cmd:
                try:
                    r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=60,
                                       **core._nowindow())
                    info["version"] = (r.stdout + r.stderr).strip().splitlines()[-1]
                except Exception:
                    pass
            self.oscad_info = info
        return self.oscad_info

    # ---------------------------------------------------------------- files
    def add_local(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isfile(path):
            raise ValueError("File not found: " + path)
        if os.path.splitext(path)[1].lower() not in SCAD_EXT:
            raise ValueError("Choose a .scad (or .csg) file")
        fid = secrets.token_hex(6)
        self.files[fid] = {"path": path, "name": os.path.basename(path), "local": True}
        rec = [path] + [p for p in core.load_settings().get("recent", []) if p != path]
        core.save_settings(recent=rec[:8])
        return fid

    def file_info(self, fid):
        f = self.files[fid]
        return {"id": fid, "name": f["name"], "path": f["path"] if f["local"] else "",
                "local": f["local"], "params": core.scan_parameters(f["path"]) if f["path"].endswith(".scad") else []}

    def _out_path(self, f, where):
        stem = os.path.splitext(f["name"])[0]
        if f["local"] and where == "beside":
            return os.path.join(os.path.dirname(f["path"]), stem + ".step")
        return _unique(os.path.join(self.out, stem + ".step"))

    # ---------------------------------------------------------------- jobs
    def start_job(self, fid, opts, values):
        f = self.files.get(fid)
        if not f:
            raise ValueError("Choose a file first")
        params = core.scan_parameters(f["path"]) if f["path"].endswith(".scad") else []
        prm = _prm_from(opts, _defines_from(values, params))
        out = self._out_path(f, opts.get("where", "beside"))
        jid = secrets.token_hex(6)
        job = {"id": jid, "state": "queued", "progress": 0.0, "log": [], "filename": os.path.basename(out),
               "out_path": out, "summary": "", "error": None, "solid": None}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, f["path"], out, prm), daemon=True).start()
        return jid

    def _run(self, job, src, out, prm):
        log = lambda s: job["log"].extend(str(s).splitlines() or [""])
        with self.run_lock:
            job["state"] = "running"
            try:
                tmp = os.path.join(self.work, job["id"] + ".step")
                rep = core.convert(src, tmp, prm, log=log, progress=lambda x: job.__setitem__("progress", x))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copyfile(tmp, out)
                obj_path = os.path.join(self.work, job["id"] + ".obj")
                try:
                    _step_to_obj(tmp, obj_path)
                    job["obj_id"] = job["id"]
                except Exception as e:
                    log(f"Note: 3D preview unavailable ({e})")
                job.update(summary=rep.summary(), solid=rep.is_solid, tmp=tmp, progress=1.0, state="done")
                log(f"Saved to {out}")
            except core.ScadError as e:
                job["error"] = str(e)
                log("ERROR: " + job["error"])
                job["state"] = "error"
            except Exception as e:  # noqa
                job["error"] = str(e) or e.__class__.__name__
                log("ERROR: " + job["error"])
                log(traceback.format_exc())
                job["state"] = "error"

    # ---------------------------------------------------------------- live link
    def start_watch(self, fid, opts, values):
        self.stop_watch()
        f = self.files.get(fid)
        if not f or not f["local"]:
            raise ValueError("The live link needs a file chosen from this computer (not dropped in)")
        params = core.scan_parameters(f["path"]) if f["path"].endswith(".scad") else []
        prm = _prm_from(opts, _defines_from(values, params))
        prm.verify = False                       # keep each update quick
        out = self._out_path(f, opts.get("where", "beside"))
        if opts.get("where") != "beside":
            out = os.path.join(self.out, os.path.splitext(f["name"])[0] + ".step")
        w = {"file": f["path"], "out": out, "stop": threading.Event(), "log": [], "runs": 0,
             "ok": None, "when": "", "summary": ""}

        def log(s):
            w["log"].extend(str(s).splitlines() or [""])
            del w["log"][:-400]

        def done(rep, err):
            w["runs"] += 1
            w["when"] = time.strftime("%H:%M:%S")
            w["ok"] = rep is not None and rep.is_solid
            w["summary"] = rep.summary() if rep else f"ERROR: {err}"

        def loop():
            core.watch(f["path"], out, prm, log=log, stop=w["stop"].is_set, on_done=done, lock=self.run_lock)
        w["thread"] = threading.Thread(target=loop, daemon=True)
        self.watcher = w
        w["thread"].start()
        return out

    def stop_watch(self):
        if self.watcher:
            self.watcher["stop"].set()
            self.watcher = None

    def reveal(self, path):
        if not path:
            return
        target = path if os.path.exists(path) else os.path.dirname(path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                subprocess.Popen(["xdg-open", target if os.path.isdir(target) else os.path.dirname(target)])
        except Exception:
            pass

    def open_in_openscad(self, path):
        cmd = core.find_openscad()
        if cmd and path and os.path.exists(path):
            subprocess.Popen(cmd + [path], **core._nowindow())


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "SCAD2STEP"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _authed(self, q=None):
            # A custom header can't be sent cross-site without a CORS preflight we never grant.
            tok = self.headers.get("X-Token") or (q or {}).get("t", [None])[0]
            return tok and secrets.compare_digest(tok, app.token)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/favicon.ico":
                b = FAVICON.encode()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if u.path == "/":
                b = PAGE.replace("__TOKEN__", app.token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
                return
            if not self._authed(q):
                return self._json({"error": "forbidden"}, 403)
            if u.path == "/api/status":
                s = core.load_settings()
                pre, app.preselect = app.preselect, None
                return self._json({"openscad": app.openscad_status(), "out": app.out,
                                   "preselect": app.file_info(pre) if pre else None,
                                   "recent": [p for p in s.get("recent", []) if os.path.exists(p)]})
            m = re.fullmatch(r"/api/job/([0-9a-f]+)", u.path)
            if m:
                job = app.jobs.get(m.group(1))
                if not job:
                    return self._json({"error": "no such job"}, 404)
                since = int(q.get("since", ["0"])[0])
                result = {k: job[k] for k in ("state", "progress", "filename", "out_path",
                                              "summary", "error", "solid")}
                if job.get("obj_id"):
                    result["obj_id"] = job["obj_id"]
                result.update({"log": job["log"][since:], "next": len(job["log"])})
                return self._json(result)
            m = re.fullmatch(r"/api/download/([0-9a-f]+)", u.path)
            if m:
                job = app.jobs.get(m.group(1))
                if not job or job["state"] != "done":
                    return self._json({"error": "not ready"}, 404)
                data = open(job["tmp"], "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/step")
                fn = job["filename"]
                ascii_fn = fn.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quote(fn)}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r"/api/preview/([0-9a-f]+)", u.path)
            if m:
                job = app.jobs.get(m.group(1))
                if not job or not job.get("obj_id") or job["state"] != "done":
                    return self._json({"error": "preview not available"}, 404)
                obj_path = os.path.join(app.work, job["obj_id"] + ".obj")
                if not os.path.exists(obj_path):
                    return self._json({"error": "preview not found"}, 404)
                data = open(obj_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/watch":
                w = app.watcher
                if not w:
                    return self._json({"on": False})
                since = int(q.get("since", ["0"])[0])
                return self._json({"on": True, "file": w["file"], "out": w["out"], "runs": w["runs"],
                                   "ok": w["ok"], "when": w["when"], "summary": w["summary"],
                                   "log": w["log"][since:] if since <= len(w["log"]) else w["log"],
                                   "next": len(w["log"])})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                return self._json({"error": "forbidden"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/upload":
                name = _safe_name(q.get("name", ["file"])[0])
                if os.path.splitext(name)[1].lower() not in SCAD_EXT:
                    self.rfile.read(n)
                    return self._json({"error": "Drop a .scad or .csg file"}, 400)
                fid = secrets.token_hex(6)
                d = os.path.join(app.work, fid)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, name)
                left = n
                with open(path, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                app.files[fid] = {"path": path, "name": name, "local": False}
                return self._json(app.file_info(fid))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                if u.path == "/api/browse":
                    p = native_open_dialog(body.get("start", ""))
                    if not p:
                        return self._json({"cancelled": True, "dialog": True})
                    return self._json(app.file_info(app.add_local(p)))
                if u.path == "/api/open-path":
                    return self._json(app.file_info(app.add_local(body.get("path", ""))))
                if u.path == "/api/rescan":
                    return self._json(app.file_info(body.get("file", "")))
                if u.path == "/api/run":
                    return self._json({"job": app.start_job(body.get("file", ""), body.get("opts", {}),
                                                             body.get("values", {}))})
                if u.path == "/api/watch/start":
                    out = app.start_watch(body.get("file", ""), body.get("opts", {}), body.get("values", {}))
                    return self._json({"ok": True, "out": out})
                if u.path == "/api/watch/stop":
                    app.stop_watch()
                    return self._json({"ok": True})
                if u.path == "/api/settings":
                    p = (body.get("openscad") or "").strip().strip('"')
                    if p and not core.find_openscad(p):
                        return self._json({"error": "OpenSCAD didn't start from that location"}, 400)
                    core.save_settings(openscad=p)
                    return self._json({"openscad": app.openscad_status(refresh=True)})
                if u.path == "/api/find-openscad":
                    return self._json({"openscad": app.openscad_status(refresh=True)})
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            m = re.fullmatch(r"/api/reveal/([0-9a-f]+)", u.path)
            if m:
                job = app.jobs.get(m.group(1))
                app.reveal(job and job["out_path"])
                return self._json({"ok": True})
            if u.path == "/api/reveal-path":
                app.reveal(body.get("path"))
                return self._json({"ok": True})
            if u.path == "/api/open-output":
                app.reveal(app.out)
                return self._json({"ok": True})
            if u.path == "/api/open-openscad":
                f = app.files.get(body.get("file", ""))
                app.open_in_openscad(f and f["path"])
                return self._json({"ok": True})
            if u.path == "/api/quit":
                app.stop_watch()
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._json({"error": "not found"}, 404)

    return H


def serve(port=0, open_browser=True, work_dir=None, out_dir=None, open_path=""):
    here = os.path.dirname(os.path.abspath(__file__))
    work_dir = work_dir or os.environ.get("SCAD2STEP_WORK") or os.path.join(here, "work")
    out_dir = out_dir or os.environ.get("SCAD2STEP_OUTPUT") or os.path.join(os.path.dirname(here), "Output")
    _lower_priority()
    app = App(work_dir, out_dir)
    if open_path:
        try:
            app.preselect = app.add_local(open_path)
        except ValueError as e:
            print(e)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"SCAD2STEP is running at {url}")
    print("Keep this window open while you use the app. Close it (or press Ctrl+C) to quit.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_watch()
        httpd.server_close()
        shutil.rmtree(work_dir, ignore_errors=True)
        print("SCAD2STEP stopped.")


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" '
           'fill="#e8a33d"/><path d="M9 21V11l7-4 7 4v10l-7 4z M9 11l7 4 7-4 M16 15v10" fill="none" '
           'stroke="#fff" stroke-width="2.2" stroke-linejoin="round"/></svg>')

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCAD2STEP</title><link rel="icon" href="/favicon.ico" type="image/svg+xml">
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#667085;--line:#d9dde5;--acc:#c77c12;--acc2:#fdf1dd;--ok:#1f9d55;--bad:#d64545;--log:#0f1420;--logink:#d6deeb}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1a1f29;--ink:#e6e9ef;--mute:#98a2b3;--line:#2c3340;--acc:#f0a93c;--acc2:#3a2c16;--log:#0b0e14;--logink:#c9d3e3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:17px;margin:0;font-weight:650}
header .sp{flex:1}
.link{background:none;border:0;color:var(--mute);cursor:pointer;font:inherit;padding:0}
.link:hover{color:var(--ink)}
main{max-width:900px;margin:0 auto;padding:18px 16px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:0 0 10px;display:flex;gap:10px;align-items:center}
.card h2 .sp{flex:1}
.drop{border:2px dashed var(--line);border-radius:10px;padding:18px;text-align:center;color:var(--mute)}
.drop.hot{border-color:var(--acc);background:var(--acc2)}
.drop b{color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin-top:10px}
label{display:flex;align-items:center;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px 28px}
.grid label{justify-content:space-between}
input[type=number],input[type=text],select{padding:5px 7px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);font:inherit}
input[type=number]{width:96px} select{max-width:180px}
input[type=text]{min-width:0}
.pathin{flex:1;min-width:220px}
input[type=range]{accent-color:var(--acc);width:140px}
.hint,small{color:var(--mute)}
.btn{border:0;background:var(--acc);color:#fff;padding:9px 20px;border-radius:9px;font:inherit;font-weight:650;cursor:pointer}
.btn:disabled{opacity:.45;cursor:default}
.btn2{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 14px;border-radius:9px;font:inherit;cursor:pointer}
.btn2:disabled{opacity:.45;cursor:default}
.btn2.on{border-color:var(--ok);color:var(--ok);font-weight:650}
.chosen{margin-top:10px;padding:10px 12px;border-radius:9px;background:var(--acc2);display:none;gap:10px;align-items:center;flex-wrap:wrap}
.chosen code{word-break:break-all}
.bar{height:6px;background:var(--line);border-radius:4px;overflow:hidden;margin:12px 0 8px}
.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .3s}
pre{background:var(--log);color:var(--logink);border-radius:9px;padding:12px;min-height:100px;max-height:300px;overflow:auto;margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.result{display:none;margin-top:12px;padding:12px;border-radius:9px;background:var(--acc2)}
.result.err{background:rgba(214,69,69,.12)}
.result pre{background:transparent;color:var(--ink);min-height:0;padding:0;margin:6px 0 10px}
.grp{grid-column:1/-1;font-weight:650;margin-top:6px;color:var(--mute)}
.pdesc{display:block;color:var(--mute);font-size:12px}
.status{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mute);display:inline-block}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.live{display:none;margin-top:12px;padding:12px;border-radius:9px;border:1px solid var(--ok)}
.hide{display:none}
.recent{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.recent button{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:20px;padding:3px 10px;font:inherit;font-size:12px;cursor:pointer}
#viewer{width:100%;height:400px;border:1px solid var(--line);border-radius:9px;background:var(--log);display:none;margin-top:12px}
.viewer-controls{display:none;gap:8px;margin-top:8px;flex-wrap:wrap;font-size:12px;color:var(--mute)}
.viewer-controls span{display:flex;gap:4px;align-items:center}
</style></head><body>
<header><h1>SCAD2STEP</h1><span class="hint">OpenSCAD → solid STEP</span><span class="sp"></span>
<button class="link" onclick="api('/api/open-output',{})">Output folder</button>
<button class="link" onclick="toggle('settings')">Settings</button>
<button class="link" onclick="quitApp()">Quit</button></header>
<main>
<div class="card hide" id="settings"><h2>Settings</h2>
 <div class="status"><span class="dot" id="os-dot"></span><span id="os-text">Looking for OpenSCAD…</span></div>
 <div class="row"><input type="text" class="pathin" id="os-path" placeholder="Path to OpenSCAD (leave empty to find it automatically)">
  <button class="btn2" onclick="saveOs()">Save</button><button class="btn2" onclick="findOs()">Search again</button></div>
 <p class="hint">Windows: …\OpenSCAD\openscad.exe · Mac: /Applications/OpenSCAD.app · Linux: /usr/bin/openscad, an AppImage, or <code>flatpak:org.openscad.OpenSCAD</code></p>
</div>
<div class="card"><h2>Model</h2>
 <div class="row" style="margin-top:0"><button class="btn2" onclick="browse()">Choose a .scad on this computer…</button>
  <input type="text" class="pathin" id="path" placeholder="…or paste its path here and press Enter"></div>
 <div class="recent" id="recent"></div>
 <div class="drop" id="drop" style="margin-top:12px"><b>Or drop a .scad / .csg here</b><br><small>Dropped files can't use libraries next to them (include/use) — choose from disk for those.</small></div>
 <div class="chosen" id="chosen"><b id="c-name"></b><code id="c-path" class="hint"></code><span class="sp"></span>
  <button class="btn2" id="b-oscad" onclick="api('/api/open-openscad',{file:cur.id})">Open in OpenSCAD</button></div>
</div>
<div class="card hide" id="pcard"><h2>Parameters <span class="sp"></span><button class="link" onclick="rescan()">Re-read file</button><button class="link" onclick="resetParams()">Reset</button></h2>
 <div class="grid" id="params"></div></div>
<div class="card"><h2>Options</h2><div class="grid">
 <label>One OpenSCAD unit is <select id="units"><option value="mm">1 mm</option><option value="inches">1 inch</option><option value="cm">1 cm</option><option value="m">1 m</option></select></label>
 <label title="Circles with an explicit $fn at or below this stay polygons — so $fn=6 nut traps stay hexagons. Everything else becomes truly round.">Keep polygons up to $fn = <input type="number" id="ffn" value="8" min="0" max="64" step="1"></label>
 <label>Save STEP <select id="where"><option value="beside">next to the .scad</option><option value="output">in the Output folder</option></select></label>
 <label title="Renders the model in OpenSCAD too and compares volume and size">Compare with OpenSCAD's render <input type="checkbox" id="verify" checked></label>
 <label title="Text and imported 2D outlines: curved stretches become smooth splines instead of many small flat faces">Smooth text outlines <input type="checkbox" id="smooth" checked></label>
</div></div>
<div class="card"><div class="row" style="margin-top:0"><button class="btn" id="b-run" onclick="run()" disabled>Convert to STEP</button>
 <button class="btn2" id="b-live" onclick="live()" disabled title="Keeps watching the file: every time you save it in OpenSCAD, the STEP is rebuilt">Live link: off</button>
 <span class="hint" id="hint">Choose a model first</span></div>
 <div class="live" id="livebox"><b>Live link on</b> — edit in OpenSCAD and save; <code id="l-out"></code> updates by itself. (It exports the file exactly as saved; the Parameters panel above applies to “Convert to STEP”.)
  <div class="hint" id="l-state"></div></div>
 <div class="bar"><i id="bar"></i></div><pre id="log"></pre>
 <div class="result" id="res"></div>
 <div id="viewer"></div>
 <div class="viewer-controls" id="viewer-controls"><span>🖱️ Drag to rotate · Scroll to zoom · Right-drag to pan</span></div></div>
</main>
<input type="file" id="pick" class="hide" accept=".scad,.csg">
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
let cur=null, params=[], liveOn=false, liveSince=0, scene=null, camera=null, renderer=null, controls=null;
function toggle(id){$(id).classList.toggle('hide')}
function initViewer(){if(scene)return;scene=new THREE.Scene();scene.background=new THREE.Color(0x0f1420);camera=new THREE.PerspectiveCamera(75,1,0.1,10000);renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});const container=$('viewer');renderer.setSize(container.clientWidth,400);renderer.setPixelRatio(window.devicePixelRatio);container.appendChild(renderer.domElement);scene.add(new THREE.AmbientLight(0xffffff,0.6));const light=new THREE.DirectionalLight(0xffffff,0.8);light.position.set(5,10,7);scene.add(light);camera.position.set(0,0,1);setupControls()}
function setupControls(){const container=$('viewer');let isDragging=false,prevX=0,prevY=0;renderer.domElement.addEventListener('mousedown',e=>{isDragging=true;prevX=e.clientX;prevY=e.clientY});renderer.domElement.addEventListener('mousemove',e=>{if(!isDragging)return;const deltaX=(e.clientX-prevX)*0.005,deltaY=(e.clientY-prevY)*0.005;if(e.buttons===1)rotateView(deltaX,deltaY);else if(e.buttons===2)panView(deltaX,deltaY);prevX=e.clientX;prevY=e.clientY});renderer.domElement.addEventListener('mouseup',()=>{isDragging=false});renderer.domElement.addEventListener('contextmenu',e=>e.preventDefault());renderer.domElement.addEventListener('wheel',e=>{e.preventDefault();const zoom=1+e.deltaY*0.001;camera.position.multiplyScalar(zoom);render()})}
function rotateView(dx,dy){const center=new THREE.Vector3();if(scene.getObjectByName('model'))center.copy(scene.getObjectByName('model').position);const pos=camera.position.clone().sub(center);const lat=Math.asin(pos.y/pos.length()),lon=Math.atan2(pos.x,pos.z);const newLat=Math.max(-Math.PI/2,Math.min(Math.PI/2,lat+dy)),newLon=lon+dx,r=pos.length();camera.position.x=center.x+r*Math.sin(newLat)*Math.sin(newLon);camera.position.y=center.y+r*Math.cos(newLat);camera.position.z=center.z+r*Math.sin(newLat)*Math.cos(newLon);camera.lookAt(center);render()}
function panView(dx,dy){const vec=new THREE.Vector3();camera.getWorldDirection(vec);const right=new THREE.Vector3();right.crossVectors(camera.up,vec).normalize();camera.position.addScaledVector(right,-dx*10);camera.position.addScaledVector(camera.up,-dy*10);render()}
function loadOBJ(objId){if(!scene)initViewer();const url='/api/preview/'+objId;fetch(url,{headers:{'X-Token':TOKEN}}).then(r=>{if(!r.ok)throw new Error('Failed to load preview');return r.text()}).then(text=>{const lines=text.split('\n');const vertices=[],faces=[];let vOffset=0;lines.forEach(line=>{line=line.trim();if(line.startsWith('v ')){const[,x,y,z]=line.split(/\s+/).map(parseFloat);vertices.push(new THREE.Vector3(x,y,z))}else if(line.startsWith('f ')){const parts=line.split(/\s+/).slice(1).map(p=>parseInt(p.split('/')[0])-1);faces.push(...parts)}});const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(vertices.flatMap(v=>[v.x,v.y,v.z])),3));geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(faces),1));geometry.computeVertexNormals();geometry.computeBoundingBox();const bb=geometry.boundingBox,center=new THREE.Vector3((bb.min.x+bb.max.x)/2,(bb.min.y+bb.max.y)/2,(bb.min.z+bb.max.z)/2),size=bb.max.clone().sub(bb.min).length();geometry.translate(-center.x,-center.y,-center.z);const material=new THREE.MeshPhongMaterial({color:0xc77c12,shininess:30});const mesh=new THREE.Mesh(geometry,material);mesh.name='model';scene.add(mesh);camera.position.set(0,0,size*1.5);camera.lookAt(0,0,0);render()}).catch(e=>{console.error('Failed to load OBJ preview:',e)})}
function render(){if(!renderer)return;renderer.render(scene,camera)}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function status(){try{const j=await api('/api/status');showOs(j.openscad);if(j.preselect&&!cur)setFile(j.preselect);
  $('recent').innerHTML=j.recent.map(p=>'<button title="'+esc(p)+'" data-p="'+esc(p)+'">'+esc(p.split(/[\\/]/).pop())+'</button>').join('');
  for(const b of $('recent').children)b.onclick=()=>openPath(b.dataset.p)}catch(e){}}
function showOs(o){$('os-dot').className='dot '+(o.found?'ok':'bad');
  $('os-text').innerHTML=o.found?('OpenSCAD found: <code>'+esc(o.cmd)+'</code> '+esc(o.version||'')):'<b>OpenSCAD not found.</b> Install it from openscad.org or enter its location below. (.csg files still convert without it.)';
  if(!o.found)$('settings').classList.remove('hide')}
async function saveOs(){try{showOs((await api('/api/settings',{openscad:$('os-path').value})).openscad)}catch(e){alert(e.message)}}
async function findOs(){showOs((await api('/api/find-openscad',{})).openscad)}
function setFile(j){cur=j;$('chosen').style.display='flex';$('c-name').textContent=j.name;$('c-path').textContent=j.path||'(dropped in — saved to the Output folder)';
  $('b-oscad').style.display=j.local?'':'none';$('b-run').disabled=false;$('b-live').disabled=!j.local;
  $('hint').textContent=j.local?'':'Live link needs a file chosen from this computer';
  if(!j.local)$('where').value='output';
  params=j.params||[];drawParams();status()}
async function browse(){try{const j=await api('/api/browse',{start:cur&&cur.path?cur.path.replace(/[^\\/]*$/,''):''});
  if(j.cancelled){$('path').focus();$('path').placeholder='No file dialog available here — paste the full path and press Enter';return}setFile(j)}catch(e){alert(e.message)}}
async function openPath(p){try{setFile(await api('/api/open-path',{path:p}))}catch(e){alert(e.message)}}
$('path').onkeydown=e=>{if(e.key==='Enter'&&$('path').value.trim())openPath($('path').value)};
async function rescan(){if(cur){const j=await api('/api/rescan',{file:cur.id});const old=values();params=j.params;drawParams(old)}}
function resetParams(){drawParams({})}
function unq(v){return v.replace(/^"|"$/g,'').replace(/\\"/g,'"')}
function drawParams(keep){keep=keep||{};const g=$('params');g.innerHTML='';$('pcard').classList.toggle('hide',!params.length);let grp=null;
  params.forEach((p,i)=>{if(p.group&&p.group!==grp){grp=p.group;g.insertAdjacentHTML('beforeend','<div class="grp">'+esc(grp)+'</div>')}
    const v=keep[p.name]!==undefined?keep[p.name]:(p.kind==='string'?unq(p.value):p.value);let inp;
    if(p.options){inp='<select id="p'+i+'">'+p.options.map((o,k)=>'<option value="'+esc(p.kind==='string'?unq(o):o)+'"'+((p.kind==='string'?unq(o):o)==v?' selected':'')+'>'+esc(unq(p.labels[k]))+'</option>').join('')+'</select>'}
    else if(p.kind==='bool'){inp='<input type="checkbox" id="p'+i+'"'+(String(v)==='true'?' checked':'')+'>'}
    else if(p.kind==='number'){inp='<input type="number" id="p'+i+'" value="'+esc(v)+'"'+(p.min!==undefined?' min="'+p.min+'" max="'+p.max+'"':'')+' step="'+(p.step||'any')+'">'}
    else inp='<input type="text" id="p'+i+'" value="'+esc(v)+'">';
    g.insertAdjacentHTML('beforeend','<label><span>'+esc(p.name)+(p.desc?'<span class="pdesc">'+esc(p.desc)+'</span>':'')+'</span>'+inp+'</label>')})}
function values(){const o={};params.forEach((p,i)=>{const e=$('p'+i);if(e)o[p.name]=e.type==='checkbox'?(e.checked?'true':'false'):e.value});return o}
function opts(){return{units:$('units').value,faceted_fn:$('ffn').value,where:$('where').value,verify:$('verify').checked,smooth_outlines:$('smooth').checked}}
const d=$('drop');d.onclick=()=>{const p=$('pick');p.onchange=()=>{if(p.files[0])upload(p.files[0]);p.value=''};p.click()};
d.ondragover=e=>{e.preventDefault();d.classList.add('hot')};d.ondragleave=()=>d.classList.remove('hot');
d.ondrop=e=>{e.preventDefault();d.classList.remove('hot');if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0])};
async function upload(file){try{const r=await fetch('/api/upload?name='+encodeURIComponent(file.name),{method:'POST',headers:{'X-Token':TOKEN},body:file});const j=await r.json();if(!r.ok)throw new Error(j.error);setFile(j)}catch(e){alert('Upload failed: '+e.message)}}
async function run(){const log=$('log'),bar=$('bar'),res=$('res'),btn=$('b-run'),viewer=$('viewer'),controls=$('viewer-controls');
  log.textContent='';bar.style.width='0';res.style.display='none';res.className='result';viewer.style.display='none';controls.style.display='none';btn.disabled=true;
  if(renderer)renderer.dispose();scene=null;camera=null;renderer=null;
  let jid;try{jid=(await api('/api/run',{file:cur.id,opts:opts(),values:values()})).job}catch(e){log.textContent=e.message;btn.disabled=false;return}
  let since=0;const poll=async()=>{let j;try{j=await api('/api/job/'+jid+'?since='+since)}catch(e){log.textContent+='\nLost connection to the app — is its window still open?';btn.disabled=false;return}
    if(j.log.length){log.textContent+=j.log.join('\n')+'\n';log.scrollTop=log.scrollHeight}since=j.next;bar.style.width=Math.round(100*j.progress)+'%';
    if(j.state==='done'){btn.disabled=false;res.style.display='block';
      res.innerHTML=(j.solid?'<b>Done</b>':'<b>Done — but the result is not a closed solid</b>')+' — saved as <code>'+esc(j.out_path)+'</code><pre>'+esc(j.summary)+'</pre>'+
       '<a class="btn" style="text-decoration:none" href="/api/download/'+jid+'?t='+TOKEN+'">Download '+esc(j.filename)+'</a> '+
       '<button class="btn2" onclick="api(\'/api/reveal/'+jid+'\',{})">Show in folder</button>';
      if(j.obj_id){viewer.style.display='block';controls.style.display='flex';loadOBJ(jid)}return}
    if(j.state==='error'){btn.disabled=false;res.style.display='block';res.className='result err';res.innerHTML='<b>Failed:</b> '+esc(j.error||'');return}
    setTimeout(poll,400)};poll()}
async function live(){try{if(liveOn){await api('/api/watch/stop',{});}else{const j=await api('/api/watch/start',{file:cur.id,opts:opts(),values:{}});$('l-out').textContent=j.out;$('log').textContent='';$('res').style.display='none';liveSince=0}}catch(e){alert(e.message)}pollLive()}
let liveTimer=null;
async function pollLive(){clearTimeout(liveTimer);let j;try{j=await api('/api/watch?since='+liveSince)}catch(e){return}
  liveOn=j.on;$('b-live').textContent='Live link: '+(liveOn?'on':'off');$('b-live').classList.toggle('on',liveOn);$('livebox').style.display=liveOn?'block':'none';
  if(liveOn){if(j.log.length){$('log').textContent+=j.log.join('\n')+'\n';$('log').scrollTop=$('log').scrollHeight}liveSince=j.next;
    $('l-state').textContent=j.runs?('Last update '+j.when+(j.ok?' — closed solid ✓':' — check the log')):'Converting…';liveTimer=setTimeout(pollLive,800)}}
async function quitApp(){try{await api('/api/quit',{})}catch(e){}document.body.innerHTML='<main><div class="card"><h2>SCAD2STEP has stopped</h2><p>You can close this tab. Start the app again from its folder.</p></div></main>'}
function animate(){if(renderer)renderer.render(scene,camera);requestAnimationFrame(animate)}
animate();
status();pollLive();
</script></body></html>"""
