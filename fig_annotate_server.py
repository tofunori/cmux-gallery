#!/usr/bin/env python3
"""Petit serveur local pour la galerie de figures (port 8787).

POST /save  {name, dataURL}  -> écrit le PNG annoté dans <projet>/annotations/,
copie le chemin dans le presse-papier, et le colle dans le panneau Claude Code
du workspace cmux actif s'il y en a un.
"""
import base64
import json
import os
import re
import subprocess
import threading
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PROJECT = os.path.realpath(os.environ.get("GALLERY_ROOT") or os.getcwd())
OUT_DIR = os.path.join(PROJECT, "annotations")
PORT = int(os.environ.get("FIG_PORT", 8787))


def find_tex_root(p):
    """Document racine d'un fichier .tex : lui-même s'il a \\documentclass,
    sinon directive % !TEX root, sinon un .tex voisin/parent qui l'inclut."""
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
    """Surface du panneau Claude Code cible.

    Priorité : (1) surface Claude sélectionnée dans le workspace actif,
    (2) n'importe quelle surface Claude du workspace actif,
    (3) session Claude vivante la plus récente (registre cmux-sessions.json).
    Les sessions Claude sont identifiées par le registre rempli par le hook
    SessionStart cmux-register.sh (PID encore vivant = session active).
    """
    # 1. registre des sessions Claude vivantes, plus récentes d'abord
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

    # 2. surfaces du workspace actif, sélectionnées d'abord
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

    # 3. repli : session Claude vivante la plus récente, où qu'elle soit
    return alive[0]


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

    def _safe_path(self, p):
        p = os.path.realpath(os.path.expanduser(p))
        root = os.path.realpath(PROJECT)
        return p if p == root or p.startswith(root + os.sep) else None

    def do_GET(self):
        if self.path.startswith("/ls?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                d = self._safe_path(q.get("dir", ["~/Documents"])[0]) or os.path.realpath(os.path.expanduser("~/Documents"))
                if not os.path.isdir(d):
                    return self._respond(404, {"error": "pas un dossier"})
                items = []
                for name in sorted(os.listdir(d), key=str.lower):
                    if name.startswith("."):
                        continue
                    p = os.path.join(d, name)
                    items.append({"name": name, "dir": os.path.isdir(p)})
                root = os.path.realpath(os.path.expanduser("~/Documents"))
                parent = os.path.dirname(d) if d != root else None
                return self._respond(200, {"path": d, "parent": parent, "items": items})
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
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/ping":
            return self._respond(200, {"ok": True, "service": "fig-annotate"})
        if self.path == "/quote":
            try:
                qf = os.path.expanduser("~/.claude/fig-last-quote.txt")
                pending = os.path.isfile(qf) and "Annotations" in open(qf).read(500) \
                    and (time.time() - os.path.getmtime(qf)) < 900
                return self._respond(200, {"pending": bool(pending)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                sp = os.path.join(PROJECT, ".fig_state.json")
                if os.path.isfile(sp):
                    with open(sp, encoding="utf-8") as f:
                        return self._respond(200, json.load(f))
                return self._respond(200, {"favs": [], "ratings": {}})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        super().do_GET()

    def do_POST(self):
        if self.path == "/clear-quote":
            try:
                open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w").close()
                return self._respond(200, {"ok": True})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                state = {"favs": sorted(set(req.get("favs", []))),
                         "ratings": {k: v for k, v in req.get("ratings", {}).items()
                                     if isinstance(v, int) and 1 <= v <= 5}}
                sp = os.path.join(PROJECT, ".fig_state.json")
                tmp = sp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=1)
                os.replace(tmp, sp)
                return self._respond(200, {"ok": True,
                                           "favs": len(state["favs"]),
                                           "ratings": len(state["ratings"])})
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
            except subprocess.TimeoutExpired:
                return self._respond(200, {"ok": False, "error": "compilation > 120 s"})
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
                    return self._respond(200, out or {"error": "pas de correspondance"})
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
                    return self._respond(200, out or {"error": "pas de correspondance"})
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
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path != "/save":
            return self._respond(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(req["name"])[0])
            data = req["dataURL"].split(",", 1)[1]
            os.makedirs(OUT_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(OUT_DIR, f"{name}_annot_{stamp}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))

            notes = req.get("notes") or []
            msg = path
            if notes:
                lignes = "\n".join(f"{n['n']}. {n['text']}" for n in notes)
                msg = f"{path}\nAnnotations (badges numerotes sur l'image) :\n{lignes}"

            subprocess.run("pbcopy", input=msg.encode(), timeout=5)
            with open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w") as f:
                f.write(msg)

            # cmux (identify + list + send) en arriere-plan : la reponse part tout de suite
            def push():
                try:
                    ref = find_claude_surface()
                    if ref:
                        subprocess.run(["cmux", "send", "--surface", ref, msg + " "],
                                       capture_output=True, timeout=5)
                except Exception:
                    pass
            threading.Thread(target=push, daemon=True).start()

            self._respond(200, {"path": path, "sentToClaude": True,
                                "clipboard": True})
        except Exception as e:
            self._respond(500, {"error": str(e)})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
