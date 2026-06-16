#!/usr/bin/env python3
"""cmux-gallery — a full-featured artifact gallery + annotation, as a cmux plugin.

Generalises an existing figures-index builder and fig-annotate server so they
work in ANY project. `run` builds the gallery, provisions the viewer assets into
the project, starts the server (a free port, cwd = project root) and opens it as
a cmux browser surface. Full functions are preserved: search · sort · folder +
format filters · archive toggle · favourites + star ratings · thumbnails ·
PDF/Markdown/code viewers · image lightbox with annotation → Claude.

Keep the `run` terminal open — it hosts the local server. Ctrl-C stops it.

Subcommands:
    build   GALLERY_ROOT=<root> build_gallery.py  +  drop viewer assets
    run     build + start the server + open the gallery in cmux (foreground)
"""
import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: resolve the PATH symlink
BUILDER = os.path.join(HERE, "build_gallery.py")
SERVER = os.path.join(HERE, "fig_annotate_server.py")
ASSETS = os.path.join(HERE, "assets")
VIEWERS = ("pdf_viewer.html", "md_viewer.html", "code_editor.html", "latex_studio.html")
OUT = "figures_index.html"


PORT_BASE = 8790  # each project gets a stable port derived from its path


def project_port(root: str) -> int:
    """A stable, per-project port (same project → same URL, bookmarkable;
    different projects coexist on different ports)."""
    h = int(hashlib.md5(os.path.realpath(root).encode()).hexdigest(), 16)
    return PORT_BASE + (h % 1000)  # 8790–9789


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_busy(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def server_project(port: int):
    """If one of our gallery servers answers on `port`, return the project root
    it serves (realpath); otherwise None. Lets `run` reuse an already-running
    server for the same project instead of spawning a duplicate on a new port."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/ping")
        r = c.getresponse()
        body = r.read()
        c.close()
        if r.status != 200:
            return None
        d = json.loads(body or b"{}")
        if d.get("service") == "fig-annotate" and d.get("project"):
            return os.path.realpath(d["project"])
    except (OSError, ValueError):
        pass
    return None


def provision_viewers(root: str) -> None:
    """Copy every bundled viewer asset (the *.html viewers + cm/, pdfjs/,
    marked.min.js …) into <root>/.fig_thumbs/, where the server serves them.
    Both files and vendor dirs are refreshed each build so a tool upgrade ships
    new CodeMirror/pdf.js to existing projects (was: dirs copied once = stale)."""
    td = os.path.join(root, ".fig_thumbs")
    os.makedirs(td, exist_ok=True)
    for name in os.listdir(ASSETS):
        src, dst = os.path.join(ASSETS, name), os.path.join(td, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def build(root: str) -> str:
    env = dict(os.environ, GALLERY_ROOT=root)
    subprocess.run([sys.executable, BUILDER], cwd=root, env=env, check=True)
    provision_viewers(root)
    return os.path.join(root, OUT)


def wait_up(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/ping")
            r = c.getresponse()
            c.close()
            if r.status == 200:
                return True
        except OSError:
            time.sleep(0.2)
    return False


def cmd_build(a) -> None:
    out = build(a.root)
    print(f"[cmux-gallery] built {out}  (+ viewers provisioned)")


def cmd_run(a) -> None:
    out = build(a.root)
    print(f"[cmux-gallery] built {out}")
    port = a.port or project_port(a.root)
    # The build above already refreshed figures_index.html + viewers. If our own
    # gallery for THIS project is already running on its stable port, reuse it
    # (the live server serves the fresh file) instead of starting a duplicate on
    # a random port — that's what was leaking a new port on every run.
    if not a.port and _port_busy(port):
        if server_project(port) == os.path.realpath(a.root):
            url = f"http://127.0.0.1:{port}/{OUT}"
            print(f"[cmux-gallery] gallery already running on :{port} → reusing it "
                  f"(rebuilt; stable URL, no duplicate server)")
            if a.open:
                res = subprocess.run(["cmux", "browser", "open", url], capture_output=True, text=True)
                print(res.stdout.strip() or res.stderr.strip())
            print(f"[cmux-gallery] gallery → {url}")
            return
        print(f"[cmux-gallery] port {port} busy (not our gallery) → using a free port", file=sys.stderr)
        port = free_port()
    env = dict(os.environ, FIG_PORT=str(port), GALLERY_ROOT=a.root)
    print(f"[cmux-gallery] starting server on :{port}  (cwd={a.root})")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # SIGTERM -> SystemExit -> finally tears down the server (no orphan)
    srv = subprocess.Popen([sys.executable, SERVER], cwd=a.root, env=env)
    try:
        if not wait_up(port):
            print("[cmux-gallery] warning: server /ping did not answer", file=sys.stderr)
        url = f"http://127.0.0.1:{port}/{OUT}"
        if a.open:
            res = subprocess.run(["cmux", "browser", "open", url], capture_output=True, text=True)
            print(res.stdout.strip() or res.stderr.strip())
        print(f"[cmux-gallery] gallery → {url}   (Ctrl-C to stop)")
        srv.wait()
    except KeyboardInterrupt:
        print("\n[cmux-gallery] stopping server")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cmux-gallery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the gallery HTML + provision viewers")
    b.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
    r = sub.add_parser("run", help="build + start server + open in cmux (foreground)")
    r.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
    r.add_argument("--port", type=int, default=0,
                   help="server port (default: a stable port derived from the project path)")
    r.add_argument("--no-open", dest="open", action="store_false",
                   help="start (or reuse) the server without opening a cmux browser tab — "
                        "for a Dock control that just keeps the server alive at launch")
    a = p.parse_args(argv)
    {"build": cmd_build, "run": cmd_run}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
