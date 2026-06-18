#!/usr/bin/env python3
"""Local server for the figure gallery (port from FIG_PORT, default 8790).

POST /save  {name, dataURL}  -> writes the annotated PNG to <project>/annotations/,
copies the path to the clipboard, and pastes it into the Claude Code panel of the
active cmux workspace if there is one.
"""
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PROJECT = os.path.realpath(os.environ.get("GALLERY_ROOT") or os.getcwd())
OUT_DIR = os.path.join(PROJECT, "annotations")
PORT = int(os.environ.get("FIG_PORT", 8790))


def find_tex_root(p):
    """Root document of a .tex file: itself if it has \\documentclass,
    else the % !TEX root directive, else a sibling/parent .tex that includes it."""
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return p
    if "\\documentclass" in txt:
        return p
    m = re.search(r"%\s*!TEX\s+root\s*=\s*(.+)", txt, re.I)
    if m:
        cand = os.path.realpath(os.path.join(os.path.dirname(p), m.group(1).strip()))
        if os.path.isfile(cand):
            return cand
    stem = os.path.splitext(os.path.basename(p))[0]
    d = os.path.dirname(p)
    for folder in (d, os.path.dirname(d)):
        try:
            for fn in os.listdir(folder):
                if not fn.endswith(".tex"):
                    continue
                cand = os.path.join(folder, fn)
                try:
                    t = open(cand, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if "\\documentclass" in t and re.search(
                        r"\\(?:input|include)\{[^}]*" + re.escape(stem), t):
                    return cand
        except Exception:
            continue
    return p


def find_claude_surface():
    """Target Claude Code panel surface.

    Priority: (1) selected Claude surface in the active workspace,
    (2) any Claude surface in the active workspace,
    (3) most-recent live Claude session (cmux-sessions.json registry).
    Claude sessions are identified via the registry filled by the
    SessionStart hook cmux-register.sh (PID still alive = active session).
    """
    # 1. registry of live Claude sessions, most recent first
    try:
        entries = json.load(open(os.path.expanduser("~/.claude/cmux-sessions.json")))
    except Exception:
        return None
    alive = []
    for e in sorted(entries, key=lambda x: -x.get("registered_at", 0)):
        pid = e.get("shell_pid")
        sid = e.get("surface_id")
        if not pid or not sid:
            continue
        try:
            os.kill(pid, 0)
            alive.append(sid.upper())
        except OSError:
            continue
    if not alive:
        return None

    # 2. surfaces in the active workspace, selected ones first
    def run(args):
        try:
            return subprocess.run(["cmux"] + args, capture_output=True,
                                  text=True, timeout=5).stdout
        except Exception:
            return ""

    ws = None
    try:
        ident = json.loads(run(["identify", "--json"]))
        ws = (ident.get("focused") or {}).get("workspace_ref")
    except Exception:
        pass

    if ws:
        lines = run(["list-pane-surfaces", "--workspace", ws,
                     "--id-format", "both"]).splitlines()
        uuids_sel, uuids_other = [], []
        for ln in lines:
            m = re.search(r"([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})", ln)
            if not m:
                continue
            (uuids_sel if "[selected]" in ln else uuids_other).append(m.group(1))
        for u in uuids_sel + uuids_other:
            if u in alive:
                return u

    # 3. fallback: most-recent live Claude session, wherever it is
    return alive[0]


VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm")  # served with HTTP Range so <video> can seek


def write_contact_sheet(out_path, files):
    """Self-contained printable HTML grid of the selected files (sips -> base64 jpeg for
    rasters/svg, a name placeholder otherwise). Open it and Print -> PDF to share."""
    RASTER = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    cells = []
    for rel, p in files[:80]:                       # cap: keep the data-URI page reasonable
        ext = os.path.splitext(p)[1].lower()
        name = html.escape(os.path.basename(p))
        thumb = '<div class="ph">' + html.escape(ext.lstrip(".").upper() or "FILE") + '</div>'
        if ext in RASTER:
            tmp = p + ".contact.jpg"
            try:
                subprocess.run(["sips", "-Z", "460", "-s", "format", "jpeg", p, "--out", tmp],
                               capture_output=True, timeout=20)
                if os.path.isfile(tmp):
                    with open(tmp, "rb") as fh:
                        thumb = '<img src="data:image/jpeg;base64,' + base64.b64encode(fh.read()).decode() + '">'
            except Exception:
                pass
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        cells.append('<figure>' + thumb + '<figcaption>' + name + '</figcaption></figure>')
    doc = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Contact sheet</title><style>'
           'body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:24px;background:#fff;color:#111}'
           'h1{font-size:15px;font-weight:600;margin:0 0 14px}'
           '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}'
           'figure{margin:0;border:1px solid #ddd;border-radius:8px;overflow:hidden;break-inside:avoid}'
           'figure img{width:100%;height:165px;object-fit:contain;background:#f6f6f6;display:block}'
           '.ph{height:165px;display:flex;align-items:center;justify-content:center;background:#f0f0f0;color:#999;font-size:13px}'
           'figcaption{font-size:10.5px;padding:6px 8px;word-break:break-all;color:#333}'
           '</style></head><body><h1>Contact sheet — ' + str(len(files)) + ' file(s)</h1>'
           '<div class="grid">' + "".join(cells) + '</div></body></html>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PROJECT, **kw)

    def log_message(self, *a):
        pass

    def _respond(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._respond(200, {})

    def _local_only(self):
        """Reject browser cross-site requests (drive-by CSRF/RCE). The gallery's own
        requests carry a loopback Origin or none; curl sends none. A page on evil.com
        carries Origin: https://evil.com and is refused."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            from urllib.parse import urlparse
            host = urlparse(origin).hostname
        except Exception:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _safe_path(self, p):
        p = os.path.realpath(os.path.expanduser(p))
        root = os.path.realpath(PROJECT)
        return p if p == root or p.startswith(root + os.sep) else None

    def translate_path(self, path):
        # SimpleHTTPRequestHandler serves symlink targets without bound-checking.
        # Pin static GETs to PROJECT with the same realpath rule as the JSON API,
        # so an in-tree symlink pointing outside the project can't be read.
        full = super().translate_path(path)
        root = os.path.realpath(PROJECT)
        rp = os.path.realpath(full)
        if rp == root or rp.startswith(root + os.sep):
            return full
        return os.path.join(root, "__forbidden_symlink_escape__")  # nonexistent -> 404

    def _serve_file(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._respond(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _serve_video(self):
        """Serve a video file with HTTP Range support so <video> can stream and seek.
        SimpleHTTPRequestHandler answers every GET with a full 200 body and no
        Accept-Ranges, which most players refuse to scrub (or to play at all)."""
        full = self.translate_path(self.path)  # pinned to PROJECT, symlink-safe
        if not os.path.isfile(full):
            return self._respond(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "video/mp4"
        fsize = os.path.getsize(full)
        start, end, partial = 0, fsize - 1, False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else fsize - 1
                else:                                  # suffix range: bytes=-N
                    start = max(0, fsize - int(e))
                if start > end or start >= fsize:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % fsize)
                    self.end_headers()
                    return
                end = min(end, fsize - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, fsize - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break                              # player aborted on seek — normal
                remaining -= len(chunk)

    def do_GET(self):
        # On-demand downscaled thumbnail for grid cards (keeps full-res images out of
        # the browser: a 4320px plot decodes to ~38MB; its 480px thumb to ~0.5MB).
        # The lightbox still loads the full original, so viewing quality is unchanged.
        if self.path.startswith("/thumb?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                src = self._safe_path(q.get("path", [""])[0])
                if not src or not os.path.isfile(src):
                    return self._respond(404, {"error": "not found"})
                try:
                    w = max(64, min(2000, int(q.get("w", ["480"])[0])))
                except ValueError:
                    w = 480
                key = hashlib.md5((os.path.realpath(src) + ":" + str(int(os.path.getmtime(src))) + ":" + str(w)).encode()).hexdigest()
                td = os.path.join(PROJECT, ".fig_thumbs")
                os.makedirs(td, exist_ok=True)
                out = os.path.join(td, "imgthumb_" + key + ".png")
                if not os.path.exists(out):
                    try:
                        subprocess.run(["sips", "-Z", str(w), "-s", "format", "png", src, "--out", out],
                                       capture_output=True, timeout=20, check=True)
                    except Exception:
                        out = src  # sips missing/failed -> serve the original (correct, just not downscaled)
                return self._serve_file(out)
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/snippet?"):
            # first lines of a text/code file, fetched lazily by visible cards
            # (keeps the snippets out of the embedded gallery data — ~3.8MB lighter).
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                src = self._safe_path(q.get("path", [""])[0])
                if not src or not os.path.isfile(src):
                    return self._respond(404, {"error": "not found"})
                try:
                    n = max(1, min(40, int(q.get("n", ["10"])[0])))
                except ValueError:
                    n = 10
                lines = []
                with open(src, encoding="utf-8", errors="replace") as f:
                    for _ in range(n):
                        ln = f.readline()
                        if not ln:
                            break
                        lines.append(ln.rstrip("\n"))
                body = ("\n".join(lines)[:600]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=300")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/ls?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                d = self._safe_path(q.get("dir", [PROJECT])[0]) or PROJECT
                if not os.path.isdir(d):
                    return self._respond(404, {"error": "not a directory"})
                items = []
                for name in sorted(os.listdir(d), key=str.lower):
                    if name.startswith("."):
                        continue
                    p = os.path.join(d, name)
                    items.append({"name": name, "dir": os.path.isdir(p)})
                root = PROJECT
                parent = os.path.dirname(d) if d != root else None
                return self._respond(200, {"path": d, "parent": parent, "items": items})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/texroot?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                root = find_tex_root(p)
                return self._respond(200, {"root": root, "pdf": root.rsplit(".", 1)[0] + ".pdf"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/raw?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p or not os.path.isfile(p):
                    self.send_response(404); self.end_headers(); return
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                ctype = "application/pdf" if p.endswith(".pdf") else "application/octet-stream"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500); self.end_headers()
            return
        if self.path.startswith("/code?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p or not os.path.isfile(p):
                    return self._respond(404, {"error": "file not found or outside the project"})
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                return self._respond(200, {"text": text, "mtime": os.path.getmtime(p), "path": p})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/ping":
            return self._respond(200, {"ok": True, "service": "fig-annotate",
                                       "project": os.path.realpath(PROJECT)})
        if self.path == "/quote":
            try:
                qf = os.path.expanduser("~/.claude/fig-last-quote.txt")
                pending = os.path.isfile(qf) and "Annotations" in open(qf).read(500) \
                    and (time.time() - os.path.getmtime(qf)) < 900
                return self._respond(200, {"pending": bool(pending)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                sp = os.path.join(PROJECT, ".fig_state.json")
                if os.path.isfile(sp):
                    with open(sp, encoding="utf-8") as f:
                        return self._respond(200, json.load(f))
                return self._respond(200, {"favs": [], "ratings": {}, "hidden": []})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/findscript?"):
            try:
                if not self._local_only():
                    return self._respond(403, {"error": "cross-origin blocked"})
                from urllib.parse import parse_qs, urlparse
                stem = (parse_qs(urlparse(self.path).query).get("stem", [""])[0] or "").strip()[:200]
                if not stem:
                    return self._respond(400, {"error": "no stem"})
                hit = None
                try:
                    # "--" stops option parsing (stem can't become an rg flag like --pre=…);
                    # --no-config ignores RIPGREP_CONFIG_PATH. -F keeps it a literal string.
                    r = subprocess.run(["rg", "-l", "--no-messages", "--no-config", "-F",
                                        "-g", "*.{py,r,R,jl,sh,ipynb}", "--", stem, PROJECT],
                                       capture_output=True, text=True, timeout=15)
                    for line in (r.stdout or "").splitlines():
                        ap = os.path.realpath(line.strip())
                        if ap.startswith(PROJECT + os.sep):
                            hit = os.path.relpath(ap, PROJECT)
                            break
                except FileNotFoundError:
                    pass            # ripgrep not installed -> client already tried a stem match
                return self._respond(200, {"script": hit})
            except (KeyError, ValueError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        from urllib.parse import urlparse as _up
        if os.path.splitext(_up(self.path).path)[1].lower() in VIDEO_EXTS:
            return self._serve_video()
        super().do_GET()

    def do_HEAD(self):
        from urllib.parse import urlparse as _up
        if os.path.splitext(_up(self.path).path)[1].lower() in VIDEO_EXTS:
            return self._serve_video()
        super().do_HEAD()

    def do_POST(self):
        if not self._local_only():
            return self._respond(403, {"error": "cross-origin blocked"})
        if self.path == "/clear-quote":
            try:
                open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w").close()
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/save-svg":
            # Overwrite an in-project .svg with an edited version (labels moved in the
            # SVG viewer's drag mode). Keeps a one-time pristine .orig.bak alongside it.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 64 * 1024 * 1024:        # 64 MB cap
                    return self._respond(413, {"error": "empty or oversized svg"})
                req = json.loads(self.rfile.read(length))
                rel = req.get("rel") or req.get("name") or ""
                svg = req.get("svg", "")
                if "<svg" not in svg[:4000]:
                    return self._respond(400, {"error": "not an svg payload"})
                dst = self._safe_path(rel)                          # pin to PROJECT, symlink-safe
                if not dst or not dst.lower().endswith(".svg") or not os.path.isfile(dst):
                    return self._respond(400, {"error": "bad or non-svg path"})
                bak = dst + ".orig.bak"
                if not os.path.exists(bak):                          # keep the pristine original once
                    shutil.copy2(dst, bak)
                tmp = dst + ".tmp." + str(os.getpid()) + "." + str(threading.get_ident())
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(svg)
                os.replace(tmp, dst)                                 # atomic
                return self._respond(200, {"ok": True, "path": os.path.relpath(dst, PROJECT)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                tags_in = req.get("tags", {})
                tags = {}
                if isinstance(tags_in, dict):
                    for k, v in tags_in.items():
                        if isinstance(v, list) and v:
                            clean = sorted({str(t).strip() for t in v if str(t).strip()})[:30]
                            if clean:
                                tags[k] = clean
                rules = sorted({str(r).strip() for r in req.get("hideRules", [])
                                if isinstance(r, str) and str(r).strip()})[:200]
                rin = req.get("ratings", {})
                rin = rin if isinstance(rin, dict) else {}
                _strs = lambda v: sorted({str(x) for x in v}) if isinstance(v, list) else []
                state = {"favs": _strs(req.get("favs", [])),
                         "ratings": {k: v for k, v in rin.items()
                                     if isinstance(v, int) and 1 <= v <= 5},
                         "hidden": _strs(req.get("hidden", [])),
                         "tags": tags,
                         "hideRules": rules}
                sp = os.path.join(PROJECT, ".fig_state.json")
                tmp = sp + ".tmp." + str(os.getpid()) + "." + str(threading.get_ident())
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=1)
                os.replace(tmp, sp)
                return self._respond(200, {"ok": True,
                                           "favs": len(state["favs"]),
                                           "ratings": len(state["ratings"]),
                                           "hidden": len(state["hidden"])})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/rescan":
            try:
                builder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_gallery.py")
                r = subprocess.run([sys.executable, builder], cwd=PROJECT,
                                   env=dict(os.environ, GALLERY_ROOT=PROJECT),
                                   capture_output=True, text=True, timeout=300)
                return self._respond(200, {"ok": r.returncode == 0,
                                           "out": (r.stdout or "")[-200:]})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                trash = os.path.expanduser("~/.Trash")
                deleted = []
                for rel in req.get("rels", []):
                    p = os.path.realpath(os.path.join(PROJECT, rel))
                    if not p.startswith(PROJECT + os.sep) or not os.path.isfile(p):
                        continue
                    dest = os.path.join(trash, os.path.basename(p))
                    i = 1
                    while os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(p))
                        dest = os.path.join(trash, f"{base}_{i}{ext}")
                        i += 1
                    os.rename(p, dest)
                    deleted.append(rel)
                return self._respond(200, {"deleted": deleted})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                mode = req.get("mode", "folder")
                files = []
                for rel in req.get("rels", []):
                    p = os.path.realpath(os.path.join(PROJECT, rel))
                    if (p == PROJECT or p.startswith(PROJECT + os.sep)) and os.path.isfile(p):
                        files.append((rel, p))
                if not files:
                    return self._respond(400, {"error": "no valid files selected"})
                exp = os.path.join(PROJECT, "_gallery_exports")
                os.makedirs(exp, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                if mode == "zip":
                    import zipfile
                    out = os.path.join(exp, "export_" + ts + ".zip")
                    seen = {}
                    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                        for rel, p in files:
                            arc = os.path.basename(p)
                            n = seen.get(arc, 0)
                            seen[arc] = n + 1
                            if n:
                                b, e = os.path.splitext(arc)
                                arc = b + "_" + str(n) + e
                            z.write(p, arc)
                elif mode == "contact":
                    out = os.path.join(exp, "contact_" + ts + ".html")
                    write_contact_sheet(out, files)
                else:
                    out = os.path.join(exp, "export_" + ts)
                    os.makedirs(out, exist_ok=True)
                    for rel, p in files:
                        dest = os.path.join(out, os.path.basename(p))
                        i = 1
                        while os.path.exists(dest):
                            b, e = os.path.splitext(os.path.basename(p))
                            dest = os.path.join(out, b + "_" + str(i) + e)
                            i += 1
                        shutil.copy2(p, dest)
                try:
                    subprocess.run(["open", "-R", out] if os.path.isfile(out) else ["open", out],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                return self._respond(200, {"ok": True, "path": os.path.relpath(out, PROJECT), "count": len(files)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = os.path.realpath(os.path.join(PROJECT, req["rel"]))
                if p.startswith(PROJECT + os.sep) and os.path.exists(p):
                    subprocess.run(["open", p], timeout=10)
                    return self._respond(200, {"ok": True})
                return self._respond(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/compile":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req["path"])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                root = find_tex_root(p)
                r = subprocess.run(
                    ["/Library/TeX/texbin/latexmk", "-pdf", "-synctex=1",
                     "-interaction=nonstopmode", "-halt-on-error",
                     os.path.basename(root)],
                    cwd=os.path.dirname(root), capture_output=True, text=True, timeout=180)
                pdf = root.rsplit(".", 1)[0] + ".pdf"
                ok = r.returncode == 0 and os.path.exists(pdf)
                log = (r.stdout or "") + (r.stderr or "")
                err = ""
                if not ok:
                    lines = [l for l in log.splitlines() if l.startswith("!") or "Error" in l]
                    err = "\n".join(lines[:8]) or log[-1500:]
                return self._respond(200, {"ok": ok, "pdf": pdf if ok else None,
                                           "root": root, "error": err})
            except FileNotFoundError:
                return self._respond(200, {"ok": False,
                                           "error": "latexmk not found at /Library/TeX/texbin/latexmk — install MacTeX or TeX Live"})
            except subprocess.TimeoutExpired:
                return self._respond(200, {"ok": False, "error": "compilation > 120 s"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/synctex":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                tex = self._safe_path(req["tex"])
                pdf = self._safe_path(req["pdf"])
                if not tex or not pdf:
                    return self._respond(403, {"error": "outside the project"})
                if req["dir"] == "view":  # source -> PDF
                    r = subprocess.run(
                        ["/Library/TeX/texbin/synctex", "view",
                         "-i", f"{req['line']}:{req.get('col',1)}:{tex}", "-o", pdf],
                        capture_output=True, text=True, timeout=10)
                    out = {}
                    for ln in r.stdout.splitlines():
                        for k in ("Page:", "x:", "y:"):
                            if ln.startswith(k):
                                out[k[:-1].lower()] = float(ln.split(":")[1])
                    return self._respond(200, out or {"error": "no match"})
                else:  # PDF -> source
                    r = subprocess.run(
                        ["/Library/TeX/texbin/synctex", "edit",
                         "-o", f"{int(req['page'])}:{req['x']}:{req['y']}:{pdf}"],
                        capture_output=True, text=True, timeout=10)
                    out = {}
                    for ln in r.stdout.splitlines():
                        if ln.startswith("Line:"):
                            out["line"] = int(ln.split(":")[1])
                        if ln.startswith("Input:"):
                            out["input"] = ln.split(":", 1)[1]
                    return self._respond(200, out or {"error": "no match"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/codesave":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req["path"])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                disk_mtime = os.path.getmtime(p) if os.path.exists(p) else 0
                if req.get("mtime") and abs(disk_mtime - req["mtime"]) > 0.001:
                    return self._respond(409, {"error": "conflit", "mtime": disk_mtime})
                with open(p, "w", encoding="utf-8") as f:
                    f.write(req["text"])
                return self._respond(200, {"mtime": os.path.getmtime(p)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/selinfo":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = os.path.expanduser("~/.claude/fig-selection.json")
                if req.get("lines"):
                    req["ts"] = time.time()
                    with open(p, "w") as f:
                        json.dump(req, f)
                elif os.path.exists(p):
                    os.remove(p)
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/quote":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                pdf = os.path.join(PROJECT, req["rel"])
                msg = f"{pdf} (p.{req['page']}) : \u00ab {req['text'].strip()} \u00bb "
                subprocess.run("pbcopy", input=msg.encode(), timeout=5)
                with open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w") as f:
                    f.write(msg)
                sent = False
                ref = find_claude_surface()
                if ref:
                    r = subprocess.run(["cmux", "send", "--surface", ref, msg],
                                       capture_output=True, timeout=5)
                    sent = r.returncode == 0
                return self._respond(200, {"sentToClaude": sent, "clipboard": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path != "/save":
            return self._respond(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(req["name"])[0])
            raw = base64.b64decode(req["dataURL"].split(",", 1)[1])  # decode FIRST: a bad dataURL must not leave a 0-byte orphan
            os.makedirs(OUT_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(OUT_DIR, f"{name}_annot_{stamp}.png")
            with open(path, "wb") as f:
                f.write(raw)

            notes = req.get("notes") or []
            msg = path
            if notes:
                lignes = "\n".join(f"{n['n']}. {n['text']}" for n in notes)
                msg = f"{path}\nAnnotations (badges numerotes sur l'image) :\n{lignes}"

            subprocess.run("pbcopy", input=msg.encode(), timeout=5)
            with open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w") as f:
                f.write(msg)

            # cmux (identify + list + send) in the background: the response returns immediately
            def push():
                try:
                    ref = find_claude_surface()
                    if ref:
                        subprocess.run(["cmux", "send", "--surface", ref, msg + " "],
                                       capture_output=True, timeout=5, start_new_session=True)
                except Exception:
                    pass
            threading.Thread(target=push, daemon=True).start()

            self._respond(200, {"path": path, "sentToClaude": True,
                                "clipboard": True})
        except Exception as e:
            self._respond(500, {"error": str(e)})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
