#!/usr/bin/env python3
"""Regenerate figures_index.html — an interactive gallery of every figure in the project.

Usage:
    GALLERY_ROOT=<project> python build_gallery.py   (or: cmux-gallery build)

Scans the project for image files (png, pdf, svg, jpg, html), collects metadata,
and writes a self-contained figures_index.html at the project root.
Run it again any time to refresh the index after producing new figures.
"""
import os, json, time, hashlib, subprocess, sys, signal, tempfile, shutil, concurrent.futures

ROOT = os.path.abspath(os.environ.get("GALLERY_ROOT") or os.getcwd())
EXTS = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".html", ".docx", ".xlsx", ".xls", ".csv", ".md", ".py", ".r", ".jl", ".tex", ".sh",
        ".mp4", ".m4v", ".mov", ".webm"}
# Skip these directories entirely (virtualenvs, git, caches, build trees, worktrees, the index itself).
# .prism is a build tree that mirrors source files (PDF/Office duplicates) — indexing it walks
# thousands of extra artefacts and thumbnails build-output copies, so exclude it outright.
EXCLUDE_PARTS = {".git", ".venv", ".venv-era5", ".venv-codex", "node_modules",
                 "__pycache__", ".ipynb_checkpoints", "worktrees", ".claude", ".fig_thumbs",
                 "_gallery_exports", ".prism"}
ARCHIVE_HINTS = ("_archive", "menage_", "/tmp/", "tmp_dir", "/tmp", "raqdps_tests")
SELF = "figures_index.html"
SNIP_EXTS = (".py", ".r", ".jl", ".sh", ".tex", ".md", ".csv")

# Animation-frame directories: hundreds of sequential stills (f000.png, frame_0001.png…).
# Hidden from the gallery by default — the playable .mp4/.gif/.html is the artifact, not the
# individual frames. Set GALLERY_SHOW_FRAMES=1 to index them anyway.
SHOW_FRAMES = bool(os.environ.get("GALLERY_SHOW_FRAMES"))

def is_frames_dir(name):
    # Still-sequence dirs only (f000.png…). NOT "*_animations" dirs — those hold the
    # playable .mp4/.gif we want to keep.
    n = name.lower()
    return (n in ("frames", "frame")
            or n.endswith(("_frames", "_frame"))
            or "html_frames" in n)


def read_snippet(path, max_lines=14, max_chars=700):
    """First lines of a text/code file, for an inline card preview."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            out = []
            for _ in range(max_lines):
                ln = f.readline()
                if not ln:
                    break
                out.append(ln.rstrip("\n"))
        return "\n".join(out)[:max_chars]
    except OSError:
        return None


def cmux_favorites():
    """Paths (relative to the project) of files already in ~/.cmux-favorites."""
    fav_dir = os.path.expanduser("~/.cmux-favorites")
    favs = set()
    if os.path.isdir(fav_dir):
        for fn in os.listdir(fav_dir):
            p = os.path.join(fav_dir, fn)
            target = os.path.realpath(p)
            if target.startswith(ROOT + os.sep):
                favs.add(os.path.relpath(target, ROOT).replace(os.sep, "/"))
    return favs


THUMB_DIR = os.path.join(ROOT, ".fig_thumbs")
NO_THUMBS = bool(os.environ.get("GALLERY_NO_THUMBS"))  # skip qlmanage thumbnails (PDF/Office) for speed
# qlmanage is fast/reliable for PDF + video, slow + flaky for Office docs
# (.xlsx/.xls/.docx hang or take 20-40s each and leak renderer processes).
# Office files are still indexed as cards — just not thumbnailed. Set
# GALLERY_OFFICE_THUMBS=1 to opt back into qlmanage thumbnails for them.
THUMB_EXTS = (".pdf", ".mp4", ".m4v", ".mov", ".webm")
if os.environ.get("GALLERY_OFFICE_THUMBS"):
    THUMB_EXTS = THUMB_EXTS + (".docx", ".xlsx", ".xls")

def thumb_key(rel, mtime):
    return hashlib.md5(f"{rel}:{mtime}".encode()).hexdigest()


def _kill_proc_tree(proc):
    """Kill a subprocess together with any descendants it spawned.

    qlmanage forks QuickLook renderer/worker processes that outlive the parent
    being killed — that is what leaves orphaned qlmanage instances after every
    rescan. With start_new_session=True the child runs in its own process group
    so we can tear the whole tree down with one killpg without touching the
    builder process."""
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def build_thumbs(pending):
    """Generate missing thumbnails in parallel, one qlmanage call per file.

    Each file gets its own temp output dir so concurrent calls with duplicate
    basenames can't clobber each other's <basename>.png. qlmanage is flaky on
    macOS (Office files / corrupt PDFs hang it), so each call runs in its own
    process group with a short timeout — killpg tears down qlmanage AND its
    renderer children instead of orphaning them."""
    if not pending:
        return
    os.makedirs(THUMB_DIR, exist_ok=True)
    # sweep stale per-file temp dirs from any prior crashed/killed run
    for name in os.listdir(THUMB_DIR):
        if name.startswith("qlm_"):
            shutil.rmtree(os.path.join(THUMB_DIR, name), ignore_errors=True)
    workers = min(8, os.cpu_count() or 4)

    def gen(job):
        full, key = job
        base = os.path.basename(full)
        out = os.path.join(THUMB_DIR, key + ".png")
        fail = os.path.join(THUMB_DIR, key + ".fail")
        tmp = tempfile.mkdtemp(prefix="qlm_", dir=THUMB_DIR)  # per-file: no basename collisions
        try:
            proc = None
            try:
                proc = subprocess.Popen(
                    ["qlmanage", "-t", "-s", "480", "-o", tmp, full],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _kill_proc_tree(proc)
                if proc is not None:
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            except Exception:
                _kill_proc_tree(proc)
            produced = os.path.join(tmp, base + ".png")
            if os.path.exists(produced):
                try:
                    os.replace(produced, out)
                    if os.path.exists(fail):
                        os.remove(fail)
                except OSError:
                    pass
            else:
                open(fail, "w").close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(gen, pending))
    print(f"[gallery] built {len(pending)} qlmanage thumbnail(s)")


def scan():
    rows = []
    thumb_pending = []
    keys_seen = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if set(dirpath.split(os.sep)) & EXCLUDE_PARTS:
            dirnames[:] = []
            continue
        if not SHOW_FRAMES:                       # don't descend into animation-frame dirs
            dirnames[:] = [d for d in dirnames if not is_frames_dir(d)]
        for fn in filenames:
            if fn.startswith("~$"):  # MS Office lock/temp files — junk, and they hang qlmanage
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in EXTS or fn == SELF:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            low = rel.lower()
            thumb = None
            if ext in THUMB_EXTS and not NO_THUMBS:
                key = thumb_key(rel, int(st.st_mtime))
                keys_seen.add(key)
                if os.path.exists(os.path.join(THUMB_DIR, key + ".png")):
                    thumb = ".fig_thumbs/" + key + ".png"
                elif not os.path.exists(os.path.join(THUMB_DIR, key + ".fail")):
                    thumb_pending.append((full, key))
                    thumb = ".fig_thumbs/" + key + ".png"
            rows.append({
                "thumb": thumb,
                "code": ext in SNIP_EXTS,  # snippet text is fetched lazily via /snippet (keeps the data light)
                "name": fn,
                "rel": rel,
                "folder": os.path.dirname(rel) or ".",
                "ext": ext.lstrip("."),
                "mtime": int(st.st_mtime),
                "btime": int(getattr(st, "st_birthtime", st.st_mtime)),
                "mdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "bdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(getattr(st, "st_birthtime", st.st_mtime))),
                "size": st.st_size,
                "archive": any(h in low for h in ARCHIVE_HINTS),
            })
    if thumb_pending:
        build_thumbs(thumb_pending)
        ok = {k for _, k in thumb_pending
              if os.path.exists(os.path.join(THUMB_DIR, k + ".png"))}
        for r in rows:
            t = r["thumb"]
            if t:
                k = t.rsplit("/", 1)[1][:-4]
                if k not in ok and not os.path.exists(os.path.join(THUMB_DIR, k + ".png")):
                    r["thumb"] = None
    purge_orphan_thumbs(keys_seen)
    rows.sort(key=lambda r: -r["mtime"])
    return rows


def purge_orphan_thumbs(keys_seen):
    """Remove .png/.fail thumbnails whose (md5) key is no longer referenced."""
    if not os.path.isdir(THUMB_DIR):
        return
    import re as _re
    pat = _re.compile(r"^([0-9a-f]{32})\.(png|fail)$")
    n = 0
    for fn in os.listdir(THUMB_DIR):
        m = pat.match(fn)
        if m and m.group(1) not in keys_seen:
            try:
                os.remove(os.path.join(THUMB_DIR, fn))
                n += 1
            except OSError:
                pass
    if n:
        print(f"  purge: {n} orphan thumbnails removed")


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{ --bg:#202024; --card:#27272a; --card2:#1f1f23; --txt:#e4e4e7; --muted:#a1a1aa;
         --accent:#5b9dff; --arch:#3a2f1a; --border:#3f3f46; }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--txt);font-size:14px}
  header{position:sticky;top:0;z-index:10;background:var(--bg);background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(8px);
         border-bottom:1px solid var(--border);padding:9px 16px}
  .brand{display:flex;align-items:baseline;gap:8px;margin-bottom:8px;flex-wrap:wrap}
  .brand .logo{color:var(--txt);align-self:center;display:block}
  .brand .wm{font-size:15px;font-weight:600;letter-spacing:.01em}
  .brand .proj{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .brand .stat{margin-left:auto;font-size:12px;color:var(--muted)}
  .controls{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  input[type=search]{flex:1;min-width:220px;padding:6px 10px;border-radius:6px;border:1px solid var(--border);
        background:var(--card);color:var(--txt);font-size:13px}
  input[type=search].collapsed{display:none}
  #searchChip.on{border-color:var(--accent);color:var(--accent)}
  select,button{padding:5px 9px;border-radius:6px;border:1px solid var(--border);background:var(--card);
        color:var(--txt);font-size:12px;cursor:pointer}
  #folder{max-width:200px}   /* keep it compact; long folder paths would otherwise stretch it to its own line */
  .chip{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;border-radius:6px;
        border:1px solid var(--border);background:var(--card);cursor:pointer;user-select:none;font-size:11.5px}
  .chip.off{opacity:.4}
  .menu{position:fixed;z-index:60;display:none;flex-direction:column;gap:2px;
        background:var(--card);border:1px solid var(--border);border-radius:10px;padding:7px;min-width:min(200px,calc(100vw - 16px));max-width:min(360px,calc(100vw - 16px));
        max-height:62vh;overflow:auto;box-shadow:0 8px 28px rgba(0,0,0,.5)}
  .menu .trow{cursor:pointer}
  .menu .tck{color:var(--accent)}
  .menu .mhd{font-size:11px;color:var(--muted);padding:3px 6px 5px}
  .menu .mi{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 7px;border-radius:6px;font-size:12.5px}
  .menu .mi:hover{background:rgba(255,255,255,.05)}
  .menu .mi.on{color:var(--accent)}
  .menu .mi.muted,.menu .mi.muted:hover{color:var(--muted);background:none}
  .menu .mi.clr{color:#ff9b9b}
  .menu .lbl{cursor:pointer;flex:1;word-break:break-all}
  .menu .lbl.mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
  .menu .ct{color:var(--muted);font-size:11px}
  .menu .x{cursor:pointer;color:#ff6b6b;padding:0 3px;flex-shrink:0}
  .menu .madd{display:flex;gap:5px;margin-top:5px;padding-top:6px;border-top:1px solid var(--border)}
  .menu .madd input{flex:1;min-width:0;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--txt);padding:5px 7px;font-size:12px}
  .menu .madd button{padding:5px 10px}
  .menu input[type=checkbox]{accent-color:var(--accent);margin-right:6px;vertical-align:-1px}
  .menu .mhd.sep{border-top:1px solid var(--border);margin-top:4px;padding-top:7px}
  .menu label.lbl{display:flex;align-items:center}
  .tags{display:flex;flex-wrap:wrap;gap:4px;margin:3px 0 0}
  .tagc{display:inline-flex;align-items:center;gap:3px;background:#33415a;color:#cfe0ff;border-radius:10px;padding:1px 7px;font-size:10.5px;cursor:pointer}
  .tagc.on{background:var(--accent);color:#06121f}
  .tagc .x{color:inherit;opacity:.55;cursor:pointer}
  .tagc .x:hover{opacity:1}
  .playbtn{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
        width:48px;height:48px;border-radius:50%;background:rgba(0,0,0,.5);color:#fff;
        display:flex;align-items:center;justify-content:center;font-size:17px;padding-left:3px;
        pointer-events:none;border:2px solid rgba(255,255,255,.9)}
  .menu .only{cursor:pointer;color:var(--muted);font-size:10.5px;flex-shrink:0;padding:0 3px}
  .menu .only:hover{color:var(--accent)}
  .menu .mlink{cursor:pointer;color:var(--muted);font-size:11.5px}
  .menu .mlink:hover{color:var(--accent)}
  .count{color:var(--muted);font-size:12px;margin-left:auto}
  main{padding:18px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;
        display:flex;flex-direction:column;transition:.12s}
  .card:hover{box-shadow:0 4px 16px rgba(0,0,0,.28);transform:translateY(-1px)}
  .thumb{height:150px;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden}
  .thumb img{max-width:100%;max-height:100%;object-fit:contain}
  .ph{height:150px;display:flex;flex-direction:column;align-items:center;justify-content:center;
      background:var(--card2);color:var(--muted);gap:6px}
  .ph .ext{font-size:30px;font-weight:700;letter-spacing:1px}
  .snip{height:150px;overflow:hidden;background:var(--card2);padding:9px 11px;
        font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:8.5px;
        line-height:1.4;color:#9aa6b6;white-space:pre;tab-size:2;
        -webkit-mask-image:linear-gradient(#000 72%,transparent);mask-image:linear-gradient(#000 72%,transparent)}
  .meta{padding:10px 12px;display:flex;flex-direction:column;gap:5px;flex:1}
  .nm{font-size:13px;font-weight:600;word-break:break-word;line-height:1.3}
  .fld{font-size:11px;color:var(--muted);word-break:break-all}
  .row{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted);margin-top:auto}
  .tag{padding:2px 7px;border-radius:5px;background:var(--card2);font-size:10px;text-transform:uppercase}
  .tag.archive{background:var(--arch);color:#d9a441}
  .tag.hid{background:#3a3a44;color:#aab}
  .tag.workflow{background:#24334a;color:#b9d4ff}
  .tag.workflow.final{background:#173d2d;color:#8ff0bd}
  .tag.workflow.rejected{background:#442225;color:#ffaaa9}
  .tag.workflow.candidate{background:#3b321d;color:#ffd978}
  .card.hid{opacity:.5}
  .card.hid:hover{opacity:1}
  .acts{display:flex;gap:5px;padding:0 12px 11px}
  .acts a,.acts button{flex:1;text-align:center;text-decoration:none;font-size:10.5px;padding:3px 4px;
        background:transparent;border:1px solid var(--border);border-radius:6px;color:var(--txt);cursor:pointer;transition:.12s}
  .acts a:hover,.acts button:hover{border-color:#5b6575;color:#fff;background:rgba(255,255,255,.04)}
  .acts .ico{flex:0 0 auto;min-width:25px;padding:3px 6px;font-size:11.5px;line-height:1}
  .acts .ico.on{color:#ffce3a;border-color:#ffce3a}
  .acts .del:hover{color:#ff9a9a;border-color:#7a2a2a;background:rgba(255,80,80,.08)}
  .selbox{position:absolute;top:6px;left:6px;z-index:4;font-size:15px;cursor:pointer;line-height:1;
        background:rgba(15,17,21,.85);border:1px solid var(--border);border-radius:6px;padding:4px 7px;user-select:none;color:var(--txt);
        opacity:0;transition:opacity .12s}
  .card:hover .selbox,.selbox.on{opacity:1}
  .selbox.on{color:#ff6b6b;border-color:#ff6b6b}
  .star{position:absolute;top:6px;right:6px;font-size:18px;cursor:pointer;line-height:1;
        background:rgba(15,17,21,.85);border:1px solid var(--border);border-radius:50%;padding:5px 6px;user-select:none;color:var(--txt)}
  .star.on{color:#ffce3a;border-color:#ffce3a}
  .rate{display:flex;gap:1px;margin-top:3px;font-size:13px;line-height:1;user-select:none}
  .rate span{cursor:pointer;color:var(--border);transition:color .1s}
  .rate span.on{color:#ffce3a}
  .rate span:hover{color:#ffe28a}
  .wfsel{width:100%;margin-top:3px;padding:3px 6px;border-radius:5px;border:1px solid var(--border);
        background:var(--card2);color:var(--muted);font-size:10.5px}
  .card{position:relative}
  .empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px}
  #lb{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;
      flex-direction:column;align-items:center;justify-content:center;cursor:zoom-out}
  #lb.show{display:flex}
  #lb img{max-width:94vw;max-height:86vh;object-fit:contain;background:#fff;border-radius:6px;cursor:zoom-in}
  #lb.fs{background:#000;cursor:none;padding:0}
  #lb.fs #lbWrap{flex:1;display:flex;align-items:center;justify-content:center;width:100%;height:100%;min-height:0}
  /* Fill the viewport in BOTH true native fullscreen (cmux/browsers) AND CSS
     pane-fill (Orca) — object-fit:contain keeps aspect ratio (no distortion).
     High-res figures scale down sharply; low-res ones fill but soften (the
     trade-off for "take the whole screen"). */
  #lb.fs img{max-width:100vw;max-height:100vh;width:100vw;height:100vh;object-fit:contain;border-radius:0;background:#000;box-shadow:none;cursor:none}
  #lb.fs #lbCap,#lb.fs .lbBtn,#lb.fs #lbClose,#lb.fs #annotBar,#lb.fs #annotNote{display:none!important}
  #lb.fs #lbFs{opacity:0;pointer-events:none;transition:opacity .22s;color:rgba(255,255,255,.75);
      background:rgba(0,0,0,.5);border-radius:8px;width:40px;height:40px;top:8px;right:8px}
  #lb.fs.fs-ui,#lb.fs.fs-ui img{cursor:default}
  #lb.fs.fs-ui #lbFs{opacity:1;pointer-events:auto}
  #lb.vw{justify-content:flex-start}
  #lb.vw #lbPdf{width:100vw !important;height:100vh !important;border-radius:0}
  #lb.vw #lbCap,#lb.vw .lbBtn,#lb.vw #lbFs{display:none}
  #lbFs{position:fixed;top:10px;right:58px;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--muted);cursor:pointer;z-index:101}
  #lbFs:hover{color:#fff}
  #lbCap{color:var(--txt);font-size:13px;margin-top:10px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;justify-content:center;max-width:94vw;text-align:center}
  #lbCap span{word-break:break-all}
  #lbCap a{color:var(--accent)}
  .lbBtn{position:fixed;top:50%;transform:translateY(-50%);font-size:34px;color:var(--muted);cursor:pointer;
         padding:18px 14px;user-select:none;z-index:101}
  .lbBtn:hover{color:#fff}
  #lbPrev{left:6px} #lbNext{right:6px}
  #annotBar{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:102;display:none;
            gap:6px;align-items:center;background:rgba(20,23,30,.95);border:1px solid var(--border);
            border-radius:10px;padding:6px 10px}
  #lb.annot #annotBar{display:flex}
  #annotBar button{padding:5px 9px;font-size:13px}
  #annotBar button.sel{background:var(--border);color:#fff;border-color:var(--border)}
  #annotBar input[type=color]{width:30px;height:28px;border:none;background:none;cursor:pointer;padding:0}
  #lbWrap{position:relative;cursor:default}
  #annotCv{position:absolute;inset:0;display:none;touch-action:none}
  #annotNote{position:fixed;z-index:103;display:none;align-items:center;gap:8px;
      background:#2a2e38;border:1px solid var(--border);border-radius:22px;padding:7px 14px;
      box-shadow:0 6px 24px rgba(0,0,0,.5)}
  #annotNote input{background:none;border:none;outline:none;color:var(--txt);font-size:13px;width:240px}
  #annotNote .del{cursor:pointer;color:#9aa3b2;font-size:15px;padding:0 2px}
  #annotNote .del:hover{color:#ff6b6b}
  #annotNote .nb{background:var(--accent);color:#fff;border-radius:50%;width:22px;height:22px;
      display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex:none}
  #lb.annot #annotCv{display:block;cursor:crosshair}
  #lbClose{position:fixed;top:10px;right:14px;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-size:24px;color:var(--muted);cursor:pointer;z-index:101}
  #cmp{position:fixed;inset:0;z-index:120;background:#0b0b0d;display:none;flex-direction:column}
  #cmp.show{display:flex}
  #cmpBar{display:flex;gap:8px;align-items:center;padding:7px 14px;background:var(--bg);border-bottom:1px solid var(--border);font-size:12.5px;color:var(--muted);flex-shrink:0}
  #cmpBar button{background:transparent;border:1px solid var(--border);border-radius:6px;color:var(--txt);padding:4px 10px;font-size:12px;cursor:pointer}
  #cmpBar button:hover{border-color:#5b6575;color:#fff}
  #cmpClose{margin-left:auto;font-size:20px;cursor:pointer;color:var(--muted);line-height:1}
  #cmpClose:hover{color:#fff}
  #cmpInner{flex:1;display:flex;flex-direction:column;gap:3px;min-height:0;padding:3px}
  #cmpInner.h{flex-direction:row}
  .cmpCell{flex:1;min-height:0;min-width:0;position:relative;background:#000;border-radius:4px;overflow:hidden;touch-action:none}
  .cmpStage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;cursor:grab}
  .cmpStage.drag{cursor:grabbing}
  .cmpCell img{max-width:100%;max-height:100%;object-fit:contain;transform-origin:center center;will-change:transform;user-select:none;pointer-events:none}
  .cmpCell .clbl{position:absolute;top:6px;left:8px;font-size:11px;color:var(--txt);background:rgba(0,0,0,.6);padding:2px 7px;border-radius:5px;max-width:88%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #confirmModal{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center}
  #confirmModal.show{display:flex}
  #confirmBox{background:var(--card2);border:1px solid var(--border);border-radius:12px;padding:20px 22px;max-width:440px;box-shadow:0 12px 50px rgba(0,0,0,.6)}
  #confirmMsg{font-size:13.5px;color:var(--txt);line-height:1.5;white-space:pre-wrap;word-break:break-word;margin-bottom:16px}
  #confirmBtns{display:flex;gap:10px;justify-content:flex-end}
  #confirmBtns button{padding:7px 16px;font-size:12.5px;border-radius:7px;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--txt)}
  #confirmCancel:hover{border-color:#5b6575;color:#fff}
  #confirmOk{background:#5c1f1f;border-color:#7a2a2a;color:#fff}
  #confirmOk:hover{background:#6e2626}
  footer{padding:20px;text-align:center;color:var(--muted);font-size:11px}
  body.fs-mode{overflow:hidden;background:#000}
  body.fs-mode header,body.fs-mode main,body.fs-mode footer{display:none!important}
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg class="logo" width="19" height="19" viewBox="0 0 24 24" fill="currentColor" aria-label="Gallery"><rect x="2" y="2" width="9.5" height="13.5" rx="1.6"/><rect x="2" y="17" width="9.5" height="5" rx="1.6"/><rect x="13.5" y="2" width="8.5" height="5" rx="1.6"/><rect x="13.5" y="9" width="8.5" height="13" rx="1.6"/></svg>
    <span class="wm">__WORDMARK__</span>
    <span class="proj">__PROJECT__</span>
    <span class="stat">__COUNT__ files · __GEN__</span>
  </div>
  <div class="controls">
    <span class="chip" id="searchChip" title="Search by name or folder (press /)">&#128269; Search</span>
    <input type="search" id="q" class="collapsed" placeholder="Search by name or folder… (Esc to close)">
    <select id="sort">
      <option value="mtime">Sort: modified (newest)</option>
      <option value="mtime_asc">Sort: modified (oldest)</option>
      <option value="btime">Sort: created (newest)</option>
      <option value="btime_asc">Sort: created (oldest)</option>
      <option value="name">Sort: name (A→Z)</option>
      <option value="size">Sort: size</option>
      <option value="rating">Sort: rating (1–5)</option>
    </select>
    <select id="folder"><option value="">All folders</option></select>
    <span class="chip" id="fmtChip">Formats &#9662;</span>
    <div id="fmtMenu" class="menu"></div>
    <span class="chip off" id="favChip">&#9733; Favorites</span>
    <span class="chip" id="tagChip" title="Filter by tag / collection">&#127991; Tags &#9662;</span>
    <div id="tagMenu" class="menu"></div>
    <span class="chip" id="collChip" title="Collections and shortlists">&#9638; Collections &#9662;</span>
    <div id="collMenu" class="menu"></div>
    <span class="chip" id="workflowChip" title="Filter by figure workflow status">&#9673; Workflow &#9662;</span>
    <div id="workflowMenu" class="menu"></div>
    <span class="chip" id="recentChip" title="Recently opened files">&#8634; Recent &#9662;</span>
    <div id="recentMenu" class="menu"></div>
    <span class="chip" id="healthChip" title="Gallery server health">&#9679; Health</span>
    <div id="healthMenu" class="menu"></div>
    <span class="chip" id="viewChip" title="View options — archives, hidden, auto-hide rules">&#9881; View &#9662;</span>
    <div id="viewMenu" class="menu"></div>
    <span id="rateFilter" style="display:none"></span>
    <button id="quoteClear" style="display:none" title="Clear the annotation pending in the Claude statusline">&#9998;&#10005; Annotation</button>
    <button id="rescan" title="Rebuild the gallery index and reload">&#8635; Rescan</button>
    <button id="cmpSel" style="display:none" title="Show the selected images stacked, to compare">&#9636; Compare (0)</button>
    <button id="hideSel" style="display:none" title="Hide the selected files from the gallery (reversible)">Hide (0)</button>
    <button id="delSel" style="display:none;background:#5c1f1f;border-color:#7a2a2a">&#128465; Delete (0)</button>
    <button id="clrSel" style="display:none" title="Clear the selection">&#10005; Clear</button>
    <button id="tagSel" style="display:none" title="Tag the selected files">&#127991; Tag (0)</button>
    <button id="collectSel" style="display:none" title="Add selected files to a collection">&#9638; Collect (0)</button>
    <button id="exportSel" style="display:none" title="Export selected: folder / zip / contact sheet">&#10515; Export (0) &#9662;</button>
    <div id="exportMenu" class="menu"></div>
  </div>
</header>
<main id="grid"></main>
<div id="lb">
  <span id="lbClose">&#10005;</span>
  <span id="lbFs" title="Plein écran (f ou double-clic)">&#9974;</span>
  <span class="lbBtn" id="lbPrev">&#8249;</span>
  <span class="lbBtn" id="lbNext">&#8250;</span>
  <div id="annotBar">
    <button data-tool="arrow" title="Arrow (1)">&#8594;</button>
    <button data-tool="rect" class="sel" title="Rectangle (2)">&#9645;</button>
    <input type="color" id="annotColor" value="#ff2d2d" title="Color">
    <button id="annotUndo" title="Undo">&#8630;</button>
    <button id="annotClear" title="Clear all">&#10006;</button>
    <button id="annotSend" title="Save the annotated PNG and paste the path into Claude Code" style="background:var(--border);color:var(--txt)">&#10148; Claude</button>
  </div>
  <div id="lbWrap"><img id="lbImg" src="" alt=""><canvas id="annotCv"></canvas></div>
  <div id="annotNote"><span class="nb">1</span><input type="text" placeholder="Add a comment... (Enter)"><span class="del" title="Delete this annotation">&#128465;</span></div>
  <iframe id="lbPdf" style="display:none;width:94vw;height:86vh;border:none;border-radius:6px;background:#fff"></iframe>
  <video id="lbVid" controls playsinline style="display:none;max-width:94vw;max-height:86vh;border-radius:6px;background:#000"></video>
  <div id="lbCap"></div>
</div>
<div id="cmp">
  <div id="cmpBar">
    <button id="cmpOrient">Layout: stacked</button>
    <button id="cmpReset">Reset zoom</button>
    <span id="cmpInfo">Esc to close</span>
    <span id="cmpClose">&#10005;</span>
  </div>
  <div id="cmpInner"></div>
</div>
<div id="confirmModal"><div id="confirmBox">
  <div id="confirmMsg"></div>
  <div id="confirmBtns"><button id="confirmCancel">Cancel</button><button id="confirmOk">Delete</button></div>
</div></div>
<footer>Double-click a thumbnail or "Open" to view the file. This file must stay at the project root for the links to work. Click Rescan to refresh.</footer>
<script>
const FILES = __DATA__;
// Filenames are untrusted: escape for text (esc) and attributes (escA), and route
// every card handler through delegation on data-* (never JS-string interpolation).
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function escA(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
document.addEventListener('click',e=>{
  const el=e.target.closest('[data-act]'); if(!el) return;
  const rel=el.dataset.rel, act=el.dataset.act;
  if(act==='fav') toggleFav(rel, el);
  else if(act==='sel') toggleSel(rel, el, e);
  else if(act==='hide') toggleHide(rel);
  else if(act==='del') delOne(rel);
  else if(act==='lb') lbOpen(rel);
  else if(act==='open') openDefault(rel);
  else if(act==='src') findScript(rel);
  else if(act==='tagf') setActiveTag(el.dataset.tag);
  else if(act==='untag') removeTag(rel, el.dataset.tag);
  else if(act==='rate') setRate(rel, +el.dataset.n, e);
  else if(act==='copy'){ navigator.clipboard.writeText(rel); el.textContent='✓'; setTimeout(()=>el.textContent='Path',1200); }
});
document.addEventListener('change',e=>{
  const el=e.target.closest('.wfsel'); if(!el) return;
  setWorkflow(el.dataset.rel, el.value);
});
const SEED_FAVS = __FAVS__;
let favs = new Set(JSON.parse(localStorage.getItem('figFavs')||'[]'));
SEED_FAVS.forEach(f=>favs.add(f));
const saveFavs = ()=>localStorage.setItem('figFavs', JSON.stringify([...favs]));
saveFavs();
const THEMES = {
  'Default':   {bg:'#202024',card:'#27272a',card2:'#1f1f23',txt:'#e4e4e7',muted:'#a1a1aa',accent:'#5b9dff',border:'#3f3f46',arch:'#3a2f1a'},
  'Codex One': {bg:'#202024',card:'#2f343f',card2:'#21252b',txt:'#abb2bf',muted:'#7f848e',accent:'#4d78cc',border:'#3e4451',arch:'#3a2f1a'},
  'Dracula':   {bg:'#282a36',card:'#343746',card2:'#21222c',txt:'#f8f8f2',muted:'#9aa0b3',accent:'#bd93f9',border:'#44475a',arch:'#3a2f1a'},
  'Nord':      {bg:'#2e3440',card:'#3b4252',card2:'#272c36',txt:'#e5e9f0',muted:'#9aa3b2',accent:'#88c0d0',border:'#434c5e',arch:'#3a2f1a'}
};
let theme = localStorage.getItem('figTheme') || 'Default';
function applyTheme(name){
  if(!THEMES[name]) name='Default';
  theme=name; try{ localStorage.setItem('figTheme',name); }catch(e){}
  const t=THEMES[name], r=document.documentElement.style;
  for(const k in t) r.setProperty('--'+k, t[k]);
}
applyTheme(theme);
let hidden = new Set(JSON.parse(localStorage.getItem('figHidden')||'[]'));
let showHidden = false;
const saveHidden = ()=>{localStorage.setItem('figHidden', JSON.stringify([...hidden]));pushState();};
function updateHideChip(){ updateViewChip(); }
function toggleHide(rel){if(hidden.has(rel))hidden.delete(rel);else hidden.add(rel);saveHidden();updateHideChip();render();}
let ratings = JSON.parse(localStorage.getItem('figRatings')||'{}');
let stateTimer=null, stateLoaded=false, pendingPush=false;
function pushState(){
  if(!stateLoaded){ pendingPush=true; return; }   // never POST before the initial /state merge lands (a partial save would clobber disk)
  clearTimeout(stateTimer);
  stateTimer=setTimeout(()=>{
    fetch('/state',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({favs:[...favs],ratings,hidden:[...hidden],tags,hideRules,collections,workflow})}).catch(()=>{});
  },400);
}
const saveRatings = ()=>{localStorage.setItem('figRatings', JSON.stringify(ratings));pushState();};
// --- tags / collections + rule-based (smart) hiding ---------------------------
let tags = JSON.parse(localStorage.getItem('figTags')||'{}');            // {rel:[tag,...]}
let activeTag = '';
let collections = JSON.parse(localStorage.getItem('figCollections')||'{}'); // {name:[rel,...]}
let activeCollection = '';
let workflow = JSON.parse(localStorage.getItem('figWorkflow')||'{}');       // {rel:status}
let workflowFilter = '';
let recents = JSON.parse(localStorage.getItem('figRecent')||'[]');
let hideRules = JSON.parse(localStorage.getItem('figHideRules')||'[]');  // glob strings
const _ruleRe = {};
function ruleToRe(g){                                  // gitignore-ish glob -> RegExp
  if(_ruleRe[g]) return _ruleRe[g];
  const onBase = !g.includes('/');                     // no slash -> match basename at any depth
  let s = g.replace(/[.+^${}()|[\\]\\\\]/g,'\\\\$&')   // escape regex specials, keep * ? for glob
           .replace(/\\*\\*/g,'@@D@@').replace(/\\*/g,'[^/]*').replace(/\\?/g,'[^/]')
           .replace(/@@D@@\\//g,'(?:.*/)?').replace(/@@D@@/g,'.*');
  const o = {re:new RegExp('^'+s+'$'), onBase}; _ruleRe[g]=o; return o;
}
function matchesRule(rel){
  if(!hideRules.length) return false;
  const base = rel.slice(rel.lastIndexOf('/')+1);
  return hideRules.some(g=>{ const o=ruleToRe(g); return o.re.test(o.onBase?base:rel); });
}
const allTags = ()=>[...new Set(Object.values(tags).flat())].sort((a,b)=>a.localeCompare(b));
function saveTags(){ localStorage.setItem('figTags', JSON.stringify(tags)); pushState(); }
function saveRules(){ localStorage.setItem('figHideRules', JSON.stringify(hideRules)); for(const k in _ruleRe) delete _ruleRe[k]; pushState(); }
fetch('/state').then(r=>r.json()).then(st=>{
  (st.favs||[]).forEach(f=>favs.add(f));
  Object.assign(ratings, st.ratings||{});
  hidden = new Set(st.hidden||[]);   // server (.fig_state.json) is authoritative — else localStorage resurrects un-hidden files
  if(st.tags) tags = st.tags;
  if(st.hideRules) hideRules = st.hideRules;
  if(st.collections) collections = st.collections;
  if(st.workflow) workflow = st.workflow;
  for(const k in _ruleRe) delete _ruleRe[k];
  localStorage.setItem('figFavs', JSON.stringify([...favs]));
  localStorage.setItem('figRatings', JSON.stringify(ratings));
  localStorage.setItem('figHidden', JSON.stringify([...hidden]));
  localStorage.setItem('figTags', JSON.stringify(tags));
  localStorage.setItem('figHideRules', JSON.stringify(hideRules));
  localStorage.setItem('figCollections', JSON.stringify(collections));
  localStorage.setItem('figWorkflow', JSON.stringify(workflow));
  document.getElementById('favChip').textContent='\u2605 Favorites ('+favs.size+')';
  updateHideChip(); buildTagChip(); buildCollectionChip(); buildWorkflowChip(); buildRecentChip(); buildHealthMenu(); buildViewMenu();
  render();
}).catch(()=>{}).finally(()=>{
  stateLoaded=true;                                 // disk state is merged in now; saves are safe
  if(pendingPush){ pendingPush=false; pushState(); }
});
function setRate(rel, n, ev){
  ev.stopPropagation();
  if(ratings[rel]===n) delete ratings[rel]; else ratings[rel]=n;
  saveRatings(); render();
}
const rateRow = rel => {
  const r = ratings[rel]||0;
  return '<div class="rate" title="Rate 1\u20135 (click again to clear)">'+
    [1,2,3,4,5].map(i=>`<span class="${i<=r?'on':''}" data-act="rate" data-rel="${escA(rel)}" data-n="${i}">${i<=r?'\u2605':'\u2606'}</span>`).join('')+'</div>';
};
let onlyFavs = false;
let rateMin = 0;
const selSet = new Set();
let lastSelRel = null;     // anchor for Shift-click range selection
let renderedRels = [];     // rels in current display order (for range math)
function updateDelBtn(){
  const b = document.getElementById('delSel');
  b.style.display = selSet.size ? '' : 'none';
  const imgs = [...selSet].filter(r => imgExt(r.split('.').pop().toLowerCase()));
  const c = document.getElementById('cmpSel');
  c.style.display = imgs.length >= 2 ? '' : 'none';
  c.textContent = '▤ Compare (' + imgs.length + ')';
  document.getElementById('clrSel').style.display = selSet.size ? '' : 'none';
  const h = document.getElementById('hideSel');
  h.style.display = selSet.size ? '' : 'none';
  h.textContent = 'Hide (' + selSet.size + ')';
  const tg = document.getElementById('tagSel');
  tg.style.display = selSet.size ? '' : 'none';
  tg.textContent = '🏷 Tag (' + selSet.size + ')';
  const co = document.getElementById('collectSel');
  co.style.display = selSet.size ? '' : 'none';
  co.textContent = '▦ Collect (' + selSet.size + ')';
  const ex = document.getElementById('exportSel');
  ex.style.display = selSet.size ? '' : 'none';
  ex.textContent = '⤓ Export (' + selSet.size + ') ▾';
  b.textContent = '🗑 Delete (' + selSet.size + ')';
}
function toggleSel(rel, el, e){
  // Shift-click selects every card between the last-clicked one and this one (display order).
  if(e && e.shiftKey && lastSelRel && lastSelRel!==rel){
    const a = renderedRels.indexOf(lastSelRel), b = renderedRels.indexOf(rel);
    if(a>=0 && b>=0){
      const lo = Math.min(a,b), hi = Math.max(a,b);
      const turnOn = !selSet.has(rel);                 // range follows the target's new state
      for(let i=lo;i<=hi;i++){ if(turnOn) selSet.add(renderedRels[i]); else selSet.delete(renderedRels[i]); }
      if(window.getSelection) window.getSelection().removeAllRanges();   // drop the blue text-drag
      updateDelBtn(); render(); return;   // anchor stays at the last plain click, so you can re-adjust the endpoint
    }
  }
  if(selSet.has(rel)){ selSet.delete(rel); el.classList.remove('on'); el.textContent='\u25A2'; }
  else{ selSet.add(rel); el.classList.add('on'); el.textContent='\u25A0'; }
  lastSelRel = rel;
  updateDelBtn();
}
// --- compare: selected images with synchronized zoom/pan ---
let cmpVert = true, cmpZoom = 1, cmpPanX = 0, cmpPanY = 0, cmpDrag = null;
function cmpApply(){
  document.querySelectorAll('#cmpInner img').forEach(img=>{
    img.style.transform = `translate(${cmpPanX}px,${cmpPanY}px) scale(${cmpZoom})`;
  });
  document.getElementById('cmpInfo').textContent = Math.round(cmpZoom*100)+'% · wheel zoom · drag pan · Esc close';
}
function cmpReset(){
  cmpZoom = 1; cmpPanX = 0; cmpPanY = 0; cmpApply();
}
function openCompare(){
  const imgs = [...selSet].filter(r => imgExt(r.split('.').pop().toLowerCase()));
  if(imgs.length < 2) return;
  const inner = document.getElementById('cmpInner');
  cmpZoom = 1; cmpPanX = 0; cmpPanY = 0;
  inner.className = cmpVert ? '' : 'h';
  inner.innerHTML = imgs.map(rel => {
    const f = FILES.find(x => x.rel === rel);
    const src = rel + (f ? '?v=' + f.mtime : '');
    return `<div class="cmpCell"><span class="clbl">${esc(rel.split('/').pop())}</span><div class="cmpStage"><img src="${escA(src)}" alt=""></div></div>`;
  }).join('');
  document.getElementById('cmp').classList.add('show');
  cmpApply();
}
function cmpClose(){ document.getElementById('cmp').classList.remove('show'); document.getElementById('cmpInner').innerHTML=''; }
document.getElementById('cmpSel').onclick = openCompare;
document.getElementById('cmpClose').onclick = cmpClose;
document.getElementById('cmpReset').onclick = cmpReset;
document.getElementById('cmpOrient').onclick = function(){
  cmpVert = !cmpVert;
  document.getElementById('cmpInner').className = cmpVert ? '' : 'h';
  this.textContent = cmpVert ? 'Layout: stacked' : 'Layout: side-by-side';
};
const cmpInnerEl = document.getElementById('cmpInner');
cmpInnerEl.addEventListener('wheel', e=>{
  if(!document.getElementById('cmp').classList.contains('show')) return;
  e.preventDefault();
  const old = cmpZoom;
  cmpZoom = Math.max(.35, Math.min(8, cmpZoom * Math.pow(1.12, -e.deltaY/80)));
  const r = cmpInnerEl.getBoundingClientRect();
  const x = e.clientX - r.left - r.width/2, y = e.clientY - r.top - r.height/2;
  cmpPanX = x - (x - cmpPanX) * (cmpZoom / old);
  cmpPanY = y - (y - cmpPanY) * (cmpZoom / old);
  cmpApply();
},{passive:false});
cmpInnerEl.addEventListener('pointerdown', e=>{
  if(!document.getElementById('cmp').classList.contains('show')) return;
  cmpDrag = {x:e.clientX,y:e.clientY,px:cmpPanX,py:cmpPanY};
  cmpInnerEl.querySelectorAll('.cmpStage').forEach(x=>x.classList.add('drag'));
  cmpInnerEl.setPointerCapture(e.pointerId);
});
cmpInnerEl.addEventListener('pointermove', e=>{
  if(!cmpDrag) return;
  cmpPanX = cmpDrag.px + e.clientX - cmpDrag.x;
  cmpPanY = cmpDrag.py + e.clientY - cmpDrag.y;
  cmpApply();
});
cmpInnerEl.addEventListener('pointerup', e=>{
  cmpDrag = null;
  cmpInnerEl.querySelectorAll('.cmpStage').forEach(x=>x.classList.remove('drag'));
  try{cmpInnerEl.releasePointerCapture(e.pointerId);}catch(_){}
});
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && document.getElementById('cmp').classList.contains('show')) cmpClose();
  if(e.key === '0' && document.getElementById('cmp').classList.contains('show')) cmpReset();
});
function confirmDialog(msg, okLabel){
  return new Promise(resolve => {
    const m = document.getElementById('confirmModal');
    document.getElementById('confirmMsg').textContent = msg;
    const ok = document.getElementById('confirmOk'), cancel = document.getElementById('confirmCancel');
    ok.textContent = okLabel || 'Delete';
    m.classList.add('show');
    function onKey(ev){ if(ev.key==='Escape'){ev.stopPropagation();done(false);} else if(ev.key==='Enter'){ev.stopPropagation();done(true);} }
    function done(v){ m.classList.remove('show'); ok.onclick = cancel.onclick = m.onclick = null; document.removeEventListener('keydown', onKey, true); resolve(v); }
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    m.onclick = e => { if(e.target.id === 'confirmModal') done(false); };
    document.addEventListener('keydown', onKey, true);
  });
}
function clearSel(){ selSet.clear(); updateDelBtn(); render(); }
document.getElementById('clrSel').onclick = clearSel;
document.getElementById('hideSel').onclick = function(){
  if(!selSet.size) return;
  selSet.forEach(rel=>hidden.add(rel));
  selSet.clear();
  saveHidden(); updateHideChip(); updateDelBtn(); render();
};
// ============ tags / collections, smart-hide rules, export, figure -> script ============
function tagsRow(rel){
  const ts = tags[rel]||[];
  if(!ts.length) return '';
  return '<div class="tags">'+ts.map(t=>
    `<span class="tagc${t===activeTag?' on':''}" data-act="tagf" data-tag="${escA(t)}" title="Filter by this tag">${esc(t)}<span class="x" data-act="untag" data-rel="${escA(rel)}" data-tag="${escA(t)}" title="Remove tag">×</span></span>`
  ).join('')+'</div>';
}
function setActiveTag(t){ activeTag = (activeTag===t)?'':t; buildTagChip(); render(); }
function removeTag(rel,t){
  if(!tags[rel]) return;
  tags[rel]=tags[rel].filter(x=>x!==t);
  if(!tags[rel].length) delete tags[rel];
  if(activeTag && !allTags().includes(activeTag)) activeTag='';
  saveTags(); buildTagChip(); render();
}
function applyTagToSel(t){
  t=(t||'').trim(); if(!t || !selSet.size) return;
  selSet.forEach(rel=>{ const a=tags[rel]||(tags[rel]=[]); if(!a.includes(t)) a.push(t); });
  saveTags(); buildTagChip(); render();
}
function deleteTagEverywhere(t){
  for(const rel in tags){ tags[rel]=tags[rel].filter(x=>x!==t); if(!tags[rel].length) delete tags[rel]; }
  if(activeTag===t) activeTag='';
  saveTags(); buildTagChip(); render();
}
const WORKFLOW_STATUSES = [['draft','Draft'],['candidate','Candidate'],['final','Final'],['rejected','Rejected']];
const allCollections = ()=>Object.keys(collections).sort((a,b)=>a.localeCompare(b));
function cleanCollection(){
  const known = new Set(FILES.map(f=>f.rel));
  for(const name of Object.keys(collections)){
    collections[name] = [...new Set((collections[name]||[]).filter(r=>known.has(r)))].sort();
    if(!collections[name].length) delete collections[name];
  }
}
function saveCollections(){ cleanCollection(); localStorage.setItem('figCollections', JSON.stringify(collections)); pushState(); }
function saveWorkflow(){ localStorage.setItem('figWorkflow', JSON.stringify(workflow)); pushState(); }
function saveRecent(){ localStorage.setItem('figRecent', JSON.stringify(recents.slice(0,30))); }
function addRecent(rel){
  recents = [rel].concat(recents.filter(r=>r!==rel)).filter(r=>FILES.some(f=>f.rel===r)).slice(0,30);
  saveRecent(); buildRecentChip();
}
function applyCollectionToSel(name){
  name=(name||'').trim(); if(!name || !selSet.size) return;
  const cur = new Set(collections[name]||[]);
  selSet.forEach(rel=>cur.add(rel));
  collections[name]=[...cur].sort();
  saveCollections(); buildCollectionChip(); render();
}
function removeCollection(name){
  delete collections[name];
  if(activeCollection===name) activeCollection='';
  saveCollections(); buildCollectionChip(); render();
}
function setActiveCollection(name){
  activeCollection = activeCollection===name ? '' : name;
  buildCollectionChip(); render();
}
function setWorkflow(rel, status){
  if(status) workflow[rel]=status; else delete workflow[rel];
  saveWorkflow(); buildWorkflowChip(); render();
}
function setWorkflowFilter(status){
  workflowFilter = workflowFilter===status ? '' : status;
  buildWorkflowChip(); render();
}
function buildCollectionChip(){
  cleanCollection();
  const chip=document.getElementById('collChip'), menu=document.getElementById('collMenu');
  if(!chip||!menu) return;
  const names=allCollections();
  chip.classList.toggle('on', !!activeCollection);
  chip.innerHTML='▦ '+(activeCollection?('Collection: '+esc(activeCollection)):'Collections')+' ▾';
  let h = activeCollection ? '<div class="mi clr" data-clear="1">Clear filter</div>' : '';
  h += names.length ? names.map(name=>{
    const n=(collections[name]||[]).length;
    return `<div class="mi${name===activeCollection?' on':''}"><span class="lbl" data-pick="${escA(name)}">${esc(name)} <span class="ct">${n}</span></span><span class="x" data-del="${escA(name)}" title="Delete collection">×</span></div>`;
  }).join('') : '<div class="mi muted">No collections yet — select files, then Collect.</div>';
  h += '<div class="madd"><input type="text" id="collQuick" placeholder="new collection"><button id="collQuickAdd">Add selected</button></div>';
  menu.innerHTML=h;
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-pick]').forEach(el=>el.onclick=()=>{ setActiveCollection(el.dataset.pick); menu.style.display='none'; });
  menu.querySelectorAll('[data-del]').forEach(el=>el.onclick=()=>removeCollection(el.dataset.del));
  const c=menu.querySelector('[data-clear]'); if(c) c.onclick=()=>{ activeCollection=''; buildCollectionChip(); render(); menu.style.display='none'; };
  const inp=menu.querySelector('#collQuick'), btn=menu.querySelector('#collQuickAdd');
  if(btn) btn.onclick=()=>{ applyCollectionToSel(inp.value); inp.value=''; buildCollectionChip(); };
  if(inp) inp.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); applyCollectionToSel(inp.value); inp.value=''; buildCollectionChip(); } };
}
function buildWorkflowChip(){
  const chip=document.getElementById('workflowChip'), menu=document.getElementById('workflowMenu');
  if(!chip||!menu) return;
  chip.classList.toggle('on', !!workflowFilter);
  const label = WORKFLOW_STATUSES.find(x=>x[0]===workflowFilter);
  chip.innerHTML='◎ '+(label?('Workflow: '+label[1]):'Workflow')+' ▾';
  const counts = {};
  Object.values(workflow).forEach(s=>counts[s]=(counts[s]||0)+1);
  menu.innerHTML=(workflowFilter?'<div class="mi clr" data-clear="1">Clear filter</div>':'')+
    WORKFLOW_STATUSES.map(([s,l])=>`<div class="mi${workflowFilter===s?' on':''}"><span class="lbl" data-wf="${s}">${l} <span class="ct">${counts[s]||0}</span></span></div>`).join('');
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-wf]').forEach(el=>el.onclick=()=>{ setWorkflowFilter(el.dataset.wf); menu.style.display='none'; });
  const c=menu.querySelector('[data-clear]'); if(c) c.onclick=()=>{ workflowFilter=''; buildWorkflowChip(); render(); menu.style.display='none'; };
}
function buildRecentChip(){
  const chip=document.getElementById('recentChip'), menu=document.getElementById('recentMenu');
  if(!chip||!menu) return;
  recents = recents.filter(r=>FILES.some(f=>f.rel===r)).slice(0,30);
  saveRecent();
  chip.innerHTML='↺ Recent'+(recents.length?' ('+recents.length+')':'')+' ▾';
  menu.innerHTML=recents.length ? recents.slice(0,15).map(rel=>{
    const f=FILES.find(x=>x.rel===rel);
    return `<div class="mi"><span class="lbl" data-open="${escA(rel)}">${esc((f&&f.name)||rel)}</span><span class="ct">${esc((f&&f.folder)||'')}</span></div>`;
  }).join('')+'<div class="mi clr" data-clear-recent="1">Clear recent</div>' : '<div class="mi muted">No recent files yet.</div>';
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-open]').forEach(el=>el.onclick=()=>{ lbOpenAny(el.dataset.open); menu.style.display='none'; });
  const c=menu.querySelector('[data-clear-recent]'); if(c)c.onclick=()=>{ recents=[]; saveRecent(); buildRecentChip(); menu.style.display='none'; };
}
function buildHealthMenu(data){
  const chip=document.getElementById('healthChip'), menu=document.getElementById('healthMenu');
  if(!chip||!menu) return;
  const ok=data&&data.ok;
  chip.classList.toggle('on', !!ok);
  chip.classList.toggle('off', data && !ok);
  chip.innerHTML=(ok?'●':'○')+' Health';
  const project=data&&data.project?data.project:'checking...';
  menu.innerHTML='<div class="mhd">Server</div>'+
    `<div class="mi"><span class="lbl">Status</span><span class="ct">${ok?'OK':(data?'Offline':'Checking')}</span></div>`+
    `<div class="mi"><span class="lbl">Project</span><span class="ct">${esc(project)}</span></div>`+
    `<div class="mi"><span class="lbl">Mode</span><span class="ct">${lbOrcaFsExitAllowed()?'Orca native viewer':'Browser fullscreen'}</span></div>`+
    '<div class="madd"><button id="healthRefresh">Refresh</button></div>';
  const b=menu.querySelector('#healthRefresh'); if(b)b.onclick=()=>checkHealth();
}
function checkHealth(){
  buildHealthMenu(null);
  fetch('/ping').then(r=>r.json()).then(j=>buildHealthMenu(j)).catch(()=>buildHealthMenu({ok:false,project:''}));
}
function buildTagChip(){
  const chip=document.getElementById('tagChip'), menu=document.getElementById('tagMenu');
  if(!chip||!menu) return;
  const ts=allTags();
  chip.classList.toggle('on', !!activeTag);
  chip.innerHTML='🏷 '+(activeTag?('Tag: '+esc(activeTag)):'Tags')+' ▾';
  let h = ts.length ? ts.map(t=>{
    const n=Object.values(tags).filter(a=>a.includes(t)).length;
    return `<div class="mi${t===activeTag?' on':''}"><span class="lbl" data-pick="${escA(t)}">${esc(t)} <span class="ct">${n}</span></span><span class="x" data-del="${escA(t)}" title="Delete this tag everywhere">×</span></div>`;
  }).join('') : '<div class="mi muted">No tags yet — select files, then Tag.</div>';
  if(activeTag) h='<div class="mi clr" data-clear="1">Clear filter</div>'+h;
  menu.innerHTML=h;
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-pick]').forEach(el=>el.onclick=()=>{ setActiveTag(el.dataset.pick); menu.style.display='none'; });
  menu.querySelectorAll('[data-del]').forEach(el=>el.onclick=()=>deleteTagEverywhere(el.dataset.del));
  const c=menu.querySelector('[data-clear]'); if(c) c.onclick=()=>{ activeTag=''; buildTagChip(); render(); menu.style.display='none'; };
}
function updateViewChip(){
  const c=document.getElementById('viewChip'); if(!c) return;
  c.classList.toggle('on', !showArch || showHidden || hideRules.length>0);  // a non-default view is active
}
function buildViewMenu(){
  const menu=document.getElementById('viewMenu'); if(!menu) return;
  updateViewChip();
  menu.innerHTML=
    '<div class="mhd">Theme</div>'+
    Object.keys(THEMES).map(n=>`<div class="mi trow" data-theme="${n}"><span class="lbl">${n}</span>${n===theme?'<span class="tck">✓</span>':''}</div>`).join('')+
    '<div class="mhd sep">View</div>'+
    `<div class="mi"><label class="lbl"><input type="checkbox" id="vArch" ${showArch?'checked':''}> Include archives</label></div>`+
    `<div class="mi"><label class="lbl"><input type="checkbox" id="vHidden" ${showHidden?'checked':''}> Show hidden${hidden.size?(' ('+hidden.size+')'):''}</label></div>`+
    '<div class="mhd sep">Auto-hide rules (glob)</div>'+
    (hideRules.length?hideRules.map(g=>`<div class="mi"><span class="lbl mono">${esc(g)}</span><span class="x" data-rm="${escA(g)}" title="Remove rule">×</span></div>`).join(''):'<div class="mi muted">No rules.</div>')+
    '<div class="madd"><input type="text" id="ruleInput" placeholder="e.g. **/_qa/** or *_preview.png"><button id="ruleAdd">Add</button></div>';
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-theme]').forEach(el=>el.onclick=()=>{ applyTheme(el.dataset.theme); buildViewMenu(); });
  menu.querySelector('#vArch').onchange=function(){ showArch=this.checked; updateViewChip(); render(); };
  menu.querySelector('#vHidden').onchange=function(){ showHidden=this.checked; updateViewChip(); render(); };
  menu.querySelectorAll('[data-rm]').forEach(el=>el.onclick=()=>{ hideRules=hideRules.filter(x=>x!==el.dataset.rm); saveRules(); buildViewMenu(); render(); });
  const inp=menu.querySelector('#ruleInput'), add=menu.querySelector('#ruleAdd');
  const doAdd=()=>{ const v=(inp.value||'').trim(); if(v && !hideRules.includes(v)){ hideRules.push(v); saveRules(); buildViewMenu(); render(); const ni=menu.querySelector('#ruleInput'); if(ni) ni.focus(); } };
  add.onclick=doAdd; inp.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); doAdd(); } };
}
function buildExportMenu(){
  const menu=document.getElementById('exportMenu');
  menu.innerHTML=[['folder','📁 Folder (copy)'],['zip','📦 Zip'],['contact','📄 Contact sheet (print → PDF)']]
    .map(([m,l])=>`<div class="mi"><span class="lbl" data-exp="${m}">${l}</span></div>`).join('');
  menu.onclick=e=>e.stopPropagation();
  menu.querySelectorAll('[data-exp]').forEach(el=>el.onclick=()=>{ menu.style.display='none'; doExport(el.dataset.exp); });
}
function doExport(mode){
  if(!selSet.size) return;
  const ex=document.getElementById('exportSel'); ex.textContent='Exporting…';
  fetch('/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,rels:[...selSet]})})
    .then(r=>r.json()).then(j=>{ ex.textContent = j&&j.ok ? ('✓ '+j.count+' → '+j.path) : ('✗ '+((j&&j.error)||'error')); setTimeout(updateDelBtn,3000); })
    .catch(()=>{ ex.textContent='✗ server off'; setTimeout(updateDelBtn,3000); });
}
function closeFloat(){ const f=document.getElementById('floatMenu'); if(f) f.remove(); }
function tagSelMenu(anchor){
  closeFloat();
  const m=document.createElement('div'); m.className='menu'; m.id='floatMenu';
  const ts=allTags();
  m.innerHTML='<div class="mhd">Tag '+selSet.size+' file(s)</div>'+
    ts.map(t=>`<div class="mi"><span class="lbl" data-apply="${escA(t)}">${esc(t)}</span></div>`).join('')+
    '<div class="madd"><input type="text" id="tagInput" placeholder="new tag…"><button id="tagApply">Add</button></div>';
  m.onclick=e=>e.stopPropagation();
  placeMenu(m, anchor);
  m.querySelectorAll('[data-apply]').forEach(el=>el.onclick=()=>{ applyTagToSel(el.dataset.apply); closeFloat(); });
  const inp=m.querySelector('#tagInput'), btn=m.querySelector('#tagApply');
  const go=()=>{ applyTagToSel(inp.value); closeFloat(); };
  btn.onclick=go; inp.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); go(); } };
  inp.focus();
}
function collectSelMenu(anchor){
  closeFloat();
  const m=document.createElement('div'); m.className='menu'; m.id='floatMenu';
  const names=allCollections();
  m.innerHTML='<div class="mhd">Collect '+selSet.size+' file(s)</div>'+
    names.map(n=>`<div class="mi"><span class="lbl" data-collect="${escA(n)}">${esc(n)} <span class="ct">${(collections[n]||[]).length}</span></span></div>`).join('')+
    '<div class="madd"><input type="text" id="collInput" placeholder="new collection"><button id="collApply">Add</button></div>';
  m.onclick=e=>e.stopPropagation();
  placeMenu(m, anchor);
  m.querySelectorAll('[data-collect]').forEach(el=>el.onclick=()=>{ applyCollectionToSel(el.dataset.collect); closeFloat(); });
  const inp=m.querySelector('#collInput'), btn=m.querySelector('#collApply');
  const go=()=>{ applyCollectionToSel(inp.value); closeFloat(); };
  btn.onclick=go; inp.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); go(); } };
  inp.focus();
}
function placeMenu(menu, anchor){
  // append to <body> to escape the sticky header's backdrop-filter containing block,
  // then position fixed and clamp inside the viewport so it never clips off-screen.
  if(menu.parentNode!==document.body) document.body.appendChild(menu);
  menu.style.display='flex';
  const r=anchor.getBoundingClientRect(), vw=document.documentElement.clientWidth;
  let left=Math.min(r.left, vw-menu.offsetWidth-8); left=Math.max(8,left);
  menu.style.left=left+'px'; menu.style.top=(r.bottom+4)+'px';
}
function menuToggle(menu, anchor){
  const open = menu.style.display==='flex';
  document.querySelectorAll('.menu').forEach(x=>x.style.display='none'); closeFloat();
  const fm=document.getElementById('fmtMenu'); if(fm) fm.style.display='none';
  if(open) return;
  placeMenu(menu, anchor);
}
function lbOpenAny(rel){
  const f=FILES.find(x=>x.rel===rel); if(!f) return false;
  const i=lbList.findIndex(x=>x.rel===rel);
  if(i>=0) lbShow(i); else { lbList=[f]; lbShow(0); }
  return true;
}
function findScript(rel){
  const stem = rel.split('/').pop().replace(/\\.[^.]+$/,'');
  const hit = FILES.find(f=>codeExt(f.ext) && f.rel.split('/').pop().replace(/\\.[^.]+$/,'')===stem);
  if(hit){ lbOpenAny(hit.rel); return; }
  fetch('/findscript?stem='+encodeURIComponent(stem)).then(r=>r.json()).then(j=>{
    if(j && j.script){ if(!lbOpenAny(j.script)) window.open('/'+j.script.split('/').map(encodeURIComponent).join('/'),'_blank'); }
    else alert('No generating script found for "'+stem+'".');
  }).catch(()=>alert('Script search failed (server off?).'));
}
document.getElementById('tagChip').onclick=e=>{ e.stopPropagation(); buildTagChip(); menuToggle(document.getElementById('tagMenu'), e.currentTarget); };
document.getElementById('collChip').onclick=e=>{ e.stopPropagation(); buildCollectionChip(); menuToggle(document.getElementById('collMenu'), e.currentTarget); };
document.getElementById('workflowChip').onclick=e=>{ e.stopPropagation(); buildWorkflowChip(); menuToggle(document.getElementById('workflowMenu'), e.currentTarget); };
document.getElementById('recentChip').onclick=e=>{ e.stopPropagation(); buildRecentChip(); menuToggle(document.getElementById('recentMenu'), e.currentTarget); };
document.getElementById('healthChip').onclick=e=>{ e.stopPropagation(); checkHealth(); menuToggle(document.getElementById('healthMenu'), e.currentTarget); };
document.getElementById('viewChip').onclick=e=>{ e.stopPropagation(); buildViewMenu(); menuToggle(document.getElementById('viewMenu'), e.currentTarget); };
document.getElementById('exportSel').onclick=e=>{ e.stopPropagation(); buildExportMenu(); menuToggle(document.getElementById('exportMenu'), e.currentTarget); };
document.getElementById('tagSel').onclick=e=>{ e.stopPropagation(); tagSelMenu(e.currentTarget); };
document.getElementById('collectSel').onclick=e=>{ e.stopPropagation(); collectSelMenu(e.currentTarget); };
document.addEventListener('click',()=>{ document.querySelectorAll('.menu').forEach(x=>x.style.display='none'); closeFloat(); });
function openDefault(rel){
  fetch('/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rel})});
}
async function delOne(rel){
  if(!await confirmDialog('Move to Trash? '+rel)) return;
  try{
    const r=await fetch('/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rels:[rel]})});
    const j=await r.json();
    (j.deleted||[]).forEach(d=>{ const i=FILES.findIndex(f=>f.rel===d); if(i>=0) FILES.splice(i,1); selSet.delete(d); });
    updateDelBtn(); render();
  }catch(e){ alert('Delete failed — is the server running?'); }
}
let lbList = [], lbIdx = -1;
const lb=()=>document.getElementById('lb');
async function lbShow(i){
  if(lbIdx>=0 && i!==lbIdx && !(await annotGuard())) return;
  if(i<0||i>=lbList.length) return;
  lbIdx=i; const f=lbList[i]; lb().classList.remove('annot');
  addRecent(f.rel);
  const isTex=f.ext==='tex', isPdf=f.ext==='pdf', isMd=f.ext==='md', isCode=codeExt(f.ext), isSvg=f.ext==='svg', isVid=videoExt(f.ext);
  const img=document.getElementById('lbImg'), pdf=document.getElementById('lbPdf'), vid=document.getElementById('lbVid');
  const vw=isPdf||isMd||isCode||isSvg;
  img.style.display=(vw||isVid)?'none':'';
  pdf.style.display=vw?'':'none';
  vid.style.display=isVid?'':'none';
  if(!isVid && vid.getAttribute('src')){vid.pause();vid.removeAttribute('src');vid.load();}  // stop playback when leaving a video
  lb().classList.toggle('vw', vw);  // full-window editor/viewer
  if(isVid){vid.src=f.rel+'?v='+f.mtime;img.src='';pdf.src='';}
  else if(isTex){pdf.src='/.fig_thumbs/latex_studio.html?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&v=__VER__';img.src='';}
  else if(isPdf){pdf.src='/.fig_thumbs/pdf_viewer.html?file='+encodeURIComponent(f.rel)+'&v=__VER__';img.src='';}
  else if(isMd){pdf.src='/.fig_thumbs/md_viewer.html?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&file='+encodeURIComponent(f.rel)+'&v=__VER__';img.src='';}
  else if(isCode){pdf.src='/.fig_thumbs/code_editor.html?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&v=__VER__';img.src='';}
  else if(isSvg){pdf.src='/.fig_thumbs/svg_viewer.html?file='+encodeURIComponent(f.rel)+'&v=__VER__';img.src='';}
  else{img.src=f.rel+'?v='+f.mtime;pdf.src='';}
  document.getElementById('lbCap').innerHTML=
    `<b>${esc(f.name)}</b><span>${esc(f.folder)}</span><span>${esc(f.mdate)}</span><a href="${escA(f.rel)}" target="_blank">open original</a>`+
    (imgExt(f.ext)&&!isSvg?` <button onclick="annotToggle()" style="margin-left:8px">&#9998; Annotate</button>`:'')+
    (isSvg?` <span style="margin-left:8px;color:#5b9dff;font-size:12px">&#9672; click elements in the plot to select them</span>`:'');
  lb().classList.add('show');
}
async function lbClose(){
  if(!(await annotGuard()))return;
  if(lb().classList.contains('fs')||fsActiveEl()) await lbFsLeave();
  const v=document.getElementById('lbVid');if(v){v.pause();v.removeAttribute('src');v.load();}
  lb().classList.remove('show');lb().classList.remove('annot');lbIdx=-1;
}
let lbFsUiTimer=0, fsLeaving=false, nativeFsOk=null, lbFsEnterGen=0, lbNativeReq=false;
function fsActiveEl(){
  return document.fullscreenElement||document.webkitFullscreenElement||null;
}
function lbOrcaFsExitAllowed(){
  let p=null; try{p=new URLSearchParams(location.search);}catch(_){}
  return !!(p&&(p.get('orcaFs')==='1'||p.get('cssFs')==='1'));
}
function lbNativeFsAllowed(){
  let p=null; try{p=new URLSearchParams(location.search);}catch(_){}
  if(p&&p.get('nativeFs')==='1') return true;   // real browsers: true whole-screen
  if(lbOrcaFsExitAllowed()) return false;       // Orca uses the server-launched native viewer
  // Orca's embedded WebKit ACCEPTS requestFullscreen() (the pane fills the whole
  // screen) but IGNORES exitFullscreen() — the pane stays stuck full-screen on
  // exit. No JS trick fixes it (tried both exit APIs + multi-frame reflow).
  // Orca is allowed into native FS only when the launcher passes ?orcaFs=1.
  // Older Orca tabs used ?cssFs=1; keep that as a legacy alias so already-open
  // gallery tabs do not remain stuck in pane-only fullscreen after upgrading.
  const brands=(navigator.userAgentData&&navigator.userAgentData.brands||[]).map(b=>b.brand).join(' ');
  const sig=[navigator.userAgent||'',navigator.vendor||'',brands].join(' ');
  if(/\b(Orca|Electron|cmux)\b/i.test(sig)) return false;
  if(window.self!==window.top) return false;
  return false;
}
async function lbOrcaNativeFullscreen(){
  if(!lbOrcaFsExitAllowed()) return null;
  const rel=(lbList[lbIdx]||{}).rel||'';
  if(!rel) return {ok:false,error:'no image selected'};
  try{
    const r=await fetch('/orca-native-fullscreen',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source:'gallery-lightbox',rel})});
    let data={}; try{data=await r.json();}catch(_){}
    if(!r.ok||!data.ok) throw new Error(data.error||('HTTP '+r.status));
    return data;
  }catch(e){
    console.warn('Orca native fullscreen failed',e);
    return {ok:false,error:String(e&&e.message||e)};
  }
}
function lbFsUiPulse(){
  const el=lb(); if(!el.classList.contains('fs')) return;
  el.classList.add('fs-ui');
  clearTimeout(lbFsUiTimer);
  lbFsUiTimer=setTimeout(()=>el.classList.remove('fs-ui'),2200);
}
function lbFsEnter(){
  const el=lb(), btn=document.getElementById('lbFs');
  el.classList.add('fs');
  btn.textContent='\u2715';
  btn.title='Quitter le plein écran (Esc, f, ou double-clic)';
  lbFsUiPulse();
}
function lbFsExit(){
  const el=lb(), btn=document.getElementById('lbFs');
  el.classList.remove('fs','fs-ui');
  clearTimeout(lbFsUiTimer);
  document.body.classList.remove('fs-mode');
  document.body.style.overflow='';
  document.documentElement.style.overflow='';
  btn.textContent='\u26f6';
  btn.title='Plein écran (f ou double-clic)';
}
function lbFsReflow(){
  // Exiting (native) fullscreen resizes the embedded webview back to its pane a
  // frame or two later; a single synchronous resize fires too early and leaves
  // Orca's split pane stuck. Nudge layout repeatedly as it settles.
  const kick=()=>{void document.body.offsetHeight;window.dispatchEvent(new Event('resize'));};
  kick();
  requestAnimationFrame(()=>{kick();requestAnimationFrame(kick);});
  [60,160,320,600].forEach(ms=>setTimeout(kick,ms));
}
async function lbFsLeave(){
  if(fsLeaving) return;
  fsLeaving=true;
  lbFsEnterGen++;
  const wasNative=!!fsActiveEl()||lbNativeReq;
  lbNativeReq=false;
  try{
    lbFsExit();
    if(wasNative){
      // Orca's embedded WebKit can honor only the prefixed exit (or ignore
      // exitFullscreen entirely) — call both, don't wait for one to throw.
      try{await document.exitFullscreen?.();}catch(_){}
      try{await document.webkitExitFullscreen?.();}catch(_){}
    }
  } finally { fsLeaving=false; lbFsReflow(); }
}
async function lbFsToggle(){
  if(fsActiveEl()||lb().classList.contains('fs')){
    await lbFsLeave(); return;
  }
  if(lbOrcaFsExitAllowed()){
    await lbOrcaNativeFullscreen();
    return;
  }
  const gen=++lbFsEnterGen;
  lbFsEnter();
  document.body.classList.add('fs-mode');
  if(!lbNativeFsAllowed()){nativeFsOk=false;return;}
  if(nativeFsOk===false) return;
  const root=document.documentElement;
  const req=root.requestFullscreen||root.webkitRequestFullscreen;
  if(!req){nativeFsOk=false;return;}
  try{
    lbNativeReq=true;
    await req.call(root);
    if(gen!==lbFsEnterGen){
      if(fsActiveEl()||lbNativeReq) await lbFsLeave();
      return;
    }
    nativeFsOk=!!fsActiveEl();
  }catch(_){nativeFsOk=false;}
}
function onFsChange(){
  if(fsLeaving) return;
  if(!fsActiveEl()&&(lb().classList.contains('fs')||document.body.classList.contains('fs-mode'))){
    void lbFsLeave();
  }
}
document.addEventListener('fullscreenchange',onFsChange);
document.addEventListener('webkitfullscreenchange',onFsChange);
document.getElementById('lbFs').onclick=e=>{e.stopPropagation();lbFsToggle();};
lb().addEventListener('mousemove',lbFsUiPulse);
document.getElementById('lbImg').addEventListener('dblclick',e=>{e.stopPropagation();lbFsToggle();});
document.addEventListener('keydown',e=>{
  if(!lb().classList.contains('show'))return;
  if(lb().classList.contains('annot')){
    if((e.metaKey||e.ctrlKey)&&e.key==='z'){e.preventDefault();document.getElementById('annotUndo').onclick();return;}
    if(e.key>='1'&&e.key<='2'){
      const b=document.querySelectorAll('#annotBar button[data-tool]')[+e.key-1];
      if(b)b.onclick(); return;
    }
    if(e.key==='Escape'){
      if(annotCur){annotCur=null;annotRedraw();return;}
      const box=document.getElementById('annotNote');
      if(box.style.display==='flex'){box.style.display='none';return;}
      annotToggle(); return;
    }
  }
  if(e.key==='f'){lbFsToggle();return;}
  if(e.key==='Escape'){
    if(lb().classList.contains('fs')||fsActiveEl()){void lbFsLeave();return;}
    lbClose();
  }
  if(e.key==='ArrowLeft')lbShow(lbIdx-1);
  if(e.key==='ArrowRight')lbShow(lbIdx+1);
});
function toggleFav(rel, el){
  if(favs.has(rel)){favs.delete(rel);el.classList.remove('on');el.textContent='\u2606';}
  else{favs.add(rel);el.classList.add('on');el.textContent='\u2605';}
  saveFavs();
  pushState();
  document.getElementById('favChip').textContent='\u2605 Favorites ('+favs.size+')';
  render();
}
const FOLDERS = __FOLDERS__;
const DEFAULT_EXTS = {png:true,jpg:true,jpeg:true,svg:true,mp4:true,m4v:true,mov:true,webm:true,pdf:false,html:false,docx:false,xlsx:false,xls:false,csv:false,md:false,py:false,r:false,jl:false,tex:false,sh:false};
const exts = Object.assign({}, DEFAULT_EXTS, JSON.parse(localStorage.getItem('figExts')||'{}'));
const saveExts = ()=>localStorage.setItem('figExts', JSON.stringify(exts));
let showArch = true;
const fmtSize = b => b>1048576?(b/1048576).toFixed(1)+' MB':b>1024?(b/1024).toFixed(0)+' KB':b+' B';
const imgExt = e => e==='png'||e==='jpg'||e==='jpeg'||e==='svg';
const videoExt = e => e==='mp4'||e==='m4v'||e==='mov'||e==='webm';
const appExt = e => e==='docx'||e==='xlsx'||e==='xls'||e==='csv';
const codeExt = e => e==='py'||e==='r'||e==='jl'||e==='tex'||e==='sh';
// All file types live in this one menu (PNG/PDF/SVG/Video are no longer standalone chips).
const TYPE_LIST = [['png','PNG'],['jpg','JPG'],['svg','SVG'],['mp4','Video (mp4/mov/webm)'],['pdf','PDF'],['html','HTML'],['docx','DOCX'],['xlsx','XLSX'],['csv','CSV'],['md','Markdown'],['py','Python'],['r','R'],['jl','Julia'],['tex','LaTeX'],['sh','Shell']];
const fmtMenu=document.getElementById('fmtMenu'), fmtChip=document.getElementById('fmtChip');
const typeGroup = e => e==='jpg'?['jpg','jpeg']:e==='mp4'?['mp4','m4v','mov','webm']:e==='xlsx'?['xlsx','xls']:[e];
function fmtChipLabel(){
  const n=TYPE_LIST.filter(([e])=>exts[e]).length;
  fmtChip.innerHTML='Formats'+(n?' ('+n+')':'')+' &#9662;';
  fmtChip.classList.toggle('off',!n);
}
function buildFmtMenu(){
  fmtMenu.innerHTML='<div class="mhd">Show file types</div>'+
    TYPE_LIST.map(([e,lab])=>`<div class="mi"><label class="lbl"><input type="checkbox" data-fmt="${e}" ${exts[e]?'checked':''}> ${lab}</label><span class="only" data-only="${e}" title="Show only this type">only</span></div>`).join('')+
    '<div class="madd" style="justify-content:space-between;gap:10px"><span class="mlink" data-fmt-all="img">Images only</span><span class="mlink" data-fmt-all="reset">Reset</span></div>';
  fmtMenu.onclick=e=>e.stopPropagation();
  fmtMenu.querySelectorAll('input[data-fmt]').forEach(cb=>{
    cb.onchange=()=>{ typeGroup(cb.dataset.fmt).forEach(k=>exts[k]=cb.checked); saveExts(); fmtChipLabel(); render(); };
  });
  fmtMenu.querySelectorAll('[data-only]').forEach(el=>{
    el.onclick=()=>{ Object.keys(exts).forEach(k=>exts[k]=false); typeGroup(el.dataset.only).forEach(k=>exts[k]=true); saveExts(); buildFmtMenu(); fmtChipLabel(); render(); };
  });
  const ia=fmtMenu.querySelector('[data-fmt-all="img"]'); if(ia) ia.onclick=()=>{ Object.keys(exts).forEach(k=>exts[k]=false); ['png','jpg','jpeg','svg'].forEach(k=>exts[k]=true); saveExts(); buildFmtMenu(); fmtChipLabel(); render(); };
  const rs=fmtMenu.querySelector('[data-fmt-all="reset"]'); if(rs) rs.onclick=()=>{ Object.assign(exts, DEFAULT_EXTS); saveExts(); buildFmtMenu(); fmtChipLabel(); render(); };
}
fmtChip.onclick=e=>{ e.stopPropagation(); buildFmtMenu(); menuToggle(fmtMenu, fmtChip); };
fmtChipLabel();

const fsel = document.getElementById('folder');
FOLDERS.forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f;fsel.appendChild(o);});

// lazily fetch code snippets only for cards that scroll into view (data stays light)
const snipObserver = new IntersectionObserver((entries,obs)=>{
  for(const e of entries){
    if(!e.isIntersecting) continue;
    const el=e.target; obs.unobserve(el);
    fetch('/snippet?path='+encodeURIComponent('__ROOT__/'+el.dataset.snip)+'&n=10')
      .then(r=>r.ok?r.text():'').then(t=>{el.textContent=t;}).catch(()=>{});
  }
},{rootMargin:'250px'});
function render(){
  const q = document.getElementById('q').value.toLowerCase().trim();
  const terms = q.split(/\\s+/).filter(Boolean);
  const sort = document.getElementById('sort').value;
  const fld = fsel.value;
  let list = FILES.filter(f=>{
    if(!exts[f.ext]) return false;
    if(!showArch && f.archive) return false;
    if(!showHidden && (hidden.has(f.rel) || matchesRule(f.rel))) return false;
    if(activeTag && !(tags[f.rel]||[]).includes(activeTag)) return false;
    if(activeCollection && !(collections[activeCollection]||[]).includes(f.rel)) return false;
    if(workflowFilter && workflow[f.rel]!==workflowFilter) return false;
    if(onlyFavs && !favs.has(f.rel)) return false;
    if(onlyFavs && rateMin && (ratings[f.rel]||0)!==rateMin) return false;
    if(fld && f.folder!==fld) return false;
    if(terms.length){ const hay=(f.rel).toLowerCase(); if(!terms.every(t=>hay.includes(t))) return false; }
    return true;
  });
  list.sort((a,b)=>{
    if(sort==='rating') return (ratings[b.rel]||0)-(ratings[a.rel]||0) || b.mtime-a.mtime;
    if(sort==='name') return a.name.localeCompare(b.name);
    if(sort==='size') return b.size-a.size;
    if(sort==='mtime_asc') return a.mtime-b.mtime;
    if(sort==='btime') return b.btime-a.btime;
    if(sort==='btime_asc') return a.btime-b.btime;
    return b.mtime-a.mtime;
  });
  lbList = list.filter(f=>imgExt(f.ext)||videoExt(f.ext)||f.ext==='pdf'||f.ext==='md'||codeExt(f.ext));
  const grid=document.getElementById('grid');
  if(!list.length){grid.innerHTML='<div class="empty">No matching files.</div>';renderedRels=[];return;}
  const MAX=600;
  const slice=list.slice(0,MAX);
  renderedRels = slice.map(f=>f.rel);   // display order for Shift-click range selection
  grid.innerHTML = slice.map(f=>{
    const isImg = imgExt(f.ext);
    const isHtml = f.ext === 'html' || f.ext === 'htm';
    // images: light downscaled thumbnail from the server (full-res stays in the lightbox);
    // html: headless-Chrome render of the page; pdf/office: build-time qlmanage thumb.
    const tsrc = (isImg || isHtml) ? '/thumb?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&w=480&v='+f.mtime : (f.thumb||null);
    const imgTag = isImg
      ? `<img loading="lazy" decoding="async" src="${escA(tsrc)}" data-full="${escA(f.rel)}?v=${f.mtime}" onerror="this.onerror=null;this.src=this.dataset.full" alt="">`
      : isHtml
      ? `<img loading="lazy" decoding="async" src="${escA(tsrc)}" alt="" onerror="this.onerror=null;this.remove()">`
      : `<img loading="lazy" decoding="async" src="${escA(tsrc)}" alt="">`;
    const thumb = f.code
      ? `<div class="snip" data-snip="${escA(f.rel)}"></div>`
      : tsrc
      ? `<div class="thumb">${imgTag}</div>`
      : `<div class="ph"><span class="ext">${esc(f.ext.toUpperCase())}</span><span style="font-size:11px">no preview</span></div>`;
    const arch = f.archive?`<span class="tag archive">archive</span>`:'';
    const isFav = favs.has(f.rel);
    const isHid = hidden.has(f.rel);
    const hidTag = isHid?`<span class="tag hid">hidden</span>`:'';
    const wf = workflow[f.rel]||'';
    const wfTag = wf?`<span class="tag workflow ${escA(wf)}">${esc(WORKFLOW_STATUSES.find(x=>x[0]===wf)?.[1]||wf)}</span>`:'';
    const wfSel = `<select class="wfsel" data-rel="${escA(f.rel)}" title="Workflow status"><option value="">Workflow: none</option>`+
      WORKFLOW_STATUSES.map(([s,l])=>`<option value="${s}" ${wf===s?'selected':''}>${l}</option>`).join('')+'</select>';
    return `<div class="card ${f.archive?'arch':''} ${isHid?'hid':''}">
      <span class="selbox ${selSet.has(f.rel)?'on':''}" data-act="sel" data-rel="${escA(f.rel)}" title="Select — Shift-click to select a range">${selSet.has(f.rel)?'■':'▢'}</span>
      ${(imgExt(f.ext)||videoExt(f.ext)||f.ext==='pdf'||f.ext==='md'||codeExt(f.ext))?`<div data-act="lb" data-rel="${escA(f.rel)}" style="cursor:zoom-in;position:relative">${videoExt(f.ext)?'<span class="playbtn">&#9654;</span>':''}${thumb}</div>`:appExt(f.ext)?`<div data-act="open" data-rel="${escA(f.rel)}" style="cursor:pointer" title="Open with default app">${thumb}</div>`:`<a href="${escA(f.rel)}" target="_blank" style="text-decoration:none">${thumb}</a>`}
      <div class="meta">
        <div class="nm">${esc(f.name)}</div>
        ${rateRow(f.rel)}
        <div class="fld">${esc(f.folder)}</div>
        ${wfSel}
        ${tagsRow(f.rel)}
        <div class="row"><span class="tag">${esc(f.ext)}</span>${arch}${hidTag}${wfTag}<span title="created ${escA(f.bdate)} \u00b7 modified ${escA(f.mdate)}">${sort.startsWith('btime')?esc(f.bdate):esc(f.mdate)}</span><span>${fmtSize(f.size)}</span></div>
      </div>
      <div class="acts">
        <button data-act="open" data-rel="${escA(f.rel)}" title="Open with default app">Open</button>
        <button data-act="copy" data-rel="${escA(f.rel)}">Path</button>
        ${(imgExt(f.ext)||f.ext==='pdf')?`<button data-act="src" data-rel="${escA(f.rel)}" title="Open the script that generated this figure">&lt;/&gt; src</button>`:''}
        <button data-act="hide" data-rel="${escA(f.rel)}" title="${isHid?'Show this file again':'Hide this file from the gallery (reversible)'}">${isHid?'Unhide':'Hide'}</button>
        <button class="ico${isFav?' on':''}" data-act="fav" data-rel="${escA(f.rel)}" title="${isFav?'Remove favorite':'Add favorite'}">${isFav?'★':'☆'}</button>
        <button class="ico del" data-act="del" data-rel="${escA(f.rel)}" title="Move to Trash">🗑</button>
      </div>
    </div>`;
  }).join('') + (list.length>MAX?`<div class="empty">… and ${list.length-MAX} more. Refine your search to see them.</div>`:'');
  grid.querySelectorAll('.snip[data-snip]').forEach(el=>snipObserver.observe(el));
}
// (format quick-chips + their video-solo handler removed — all types now live in the Formats menu)
updateViewChip();
const favChip=document.getElementById('favChip');
favChip.textContent='\u2605 Favorites ('+favs.size+')';
const rateFilter=document.getElementById('rateFilter');
rateFilter.innerHTML=[1,2,3,4,5].map(n=>`<span class="chip off rf" data-n="${n}" title="Show only ${n}-star items">${n}\u2605</span>`).join('');
rateFilter.querySelectorAll('.rf').forEach(c=>{
  c.onclick=()=>{
    const n=+c.dataset.n;
    rateMin = rateMin===n ? 0 : n;
    rateFilter.querySelectorAll('.rf').forEach(x=>{const on=+x.dataset.n===rateMin;x.classList.toggle('on',on);x.classList.toggle('off',!on);});
    render();
  };
});
favChip.onclick=()=>{onlyFavs=!onlyFavs;favChip.classList.toggle('off',!onlyFavs);favChip.classList.toggle('on',onlyFavs);rateFilter.style.display=onlyFavs?'inline-flex':'none';if(!onlyFavs){rateMin=0;rateFilter.querySelectorAll('.rf').forEach(x=>{x.classList.remove('on');x.classList.add('off');});}render();};
const quoteBtn=document.getElementById('quoteClear');
function quoteCheck(){fetch('/quote').then(r=>r.json()).then(j=>{quoteBtn.style.display=j.pending?'':'none';}).catch(()=>{});}
quoteCheck(); setInterval(quoteCheck, 30000);
checkHealth(); setInterval(checkHealth, 60000);
quoteBtn.onclick=async()=>{await fetch('/clear-quote',{method:'POST'}).catch(()=>{}); quoteBtn.style.display='none';};
document.getElementById('rescan').onclick=async function(){
  this.textContent='\u23f3 scanning\u2026';
  try{
    const r=await fetch('/rescan',{method:'POST'});
    const j=await r.json();
    if(j.ok){ location.reload(); return; }
    this.textContent='\u2717 error';
  }catch(e){ this.textContent='server off'; }
  setTimeout(()=>this.textContent='\u21bb Rescan',2000);
};
document.getElementById('delSel').onclick=async function(){
  if(!selSet.size) return;
  if(!await confirmDialog(selSet.size+' file(s) \u2192 trash?')) return;
  const r=await fetch('/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({rels:[...selSet]})});
  const j=await r.json();
  (j.deleted||[]).forEach(rel=>{
    const i=FILES.findIndex(f=>f.rel===rel);
    if(i>=0) FILES.splice(i,1);
    selSet.delete(rel);
  });
  updateDelBtn(); render();
};
document.getElementById('q').oninput=render;
document.getElementById('sort').onchange=render;
fsel.onchange=render;
// collapsible search: a 🔍 chip expands the field; Esc / blur-when-empty collapses it; "/" opens it
const qEl=document.getElementById('q'), searchChip=document.getElementById('searchChip');
function openSearch(){qEl.classList.remove('collapsed');searchChip.classList.add('on');qEl.focus();qEl.select();}
function closeSearch(){const had=qEl.value;qEl.value='';qEl.classList.add('collapsed');searchChip.classList.remove('on');if(had)render();}
searchChip.onclick=()=>{ if(qEl.classList.contains('collapsed')) openSearch(); else closeSearch(); };
qEl.addEventListener('keydown',e=>{ if(e.key==='Escape'){e.preventDefault();closeSearch();searchChip.focus();} });
qEl.addEventListener('blur',()=>{ if(!qEl.value.trim()){qEl.classList.add('collapsed');searchChip.classList.remove('on');} });
document.addEventListener('keydown',e=>{
  if(e.key!=='/'||e.metaKey||e.ctrlKey||e.altKey)return;
  if(lb().classList.contains('show'))return;
  const t=e.target, tag=t&&t.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||(t&&t.isContentEditable))return;
  e.preventDefault(); openSearch();
});
// ---- Annotation ----
let annotTool='rect', annotStrokes=[], annotCur=null, annotSent=true;
async function annotGuard(){
  if(lb().classList.contains('annot') && annotStrokes.length && !annotSent)
    return await confirmDialog('Discard unsaved annotations?', 'Discard');
  return true;
}
const cv=()=>document.getElementById('annotCv');
async function annotToggle(){
  if(lb().classList.contains('annot') && !(await annotGuard())) return;
  const on=lb().classList.toggle('annot');
  if(on){
    const img=document.getElementById('lbImg');
    const c=cv();
    c.width=img.naturalWidth; c.height=img.naturalHeight;
    c.style.width=img.clientWidth+'px'; c.style.height=img.clientHeight+'px';
    annotStrokes=[]; annotSent=true; annotRedraw();
  } else { document.getElementById('annotNote').style.display='none'; }
}
function annotPos(e){
  const c=cv(), r=c.getBoundingClientRect();
  return {x:(e.clientX-r.left)*c.width/r.width, y:(e.clientY-r.top)*c.height/r.height};
}
function annotRedraw(){
  const c=cv(), x=c.getContext('2d');
  x.clearRect(0,0,c.width,c.height);
  const lw=Math.max(2,c.width/300);
  for(const s of annotStrokes.concat(annotCur?[annotCur]:[])){
    x.strokeStyle=s.color; x.fillStyle=s.color; x.lineWidth=lw; x.lineCap='round'; x.lineJoin='round';
    if(s.tool==='pen'){
      x.beginPath(); s.pts.forEach((p,i)=>i?x.lineTo(p.x,p.y):x.moveTo(p.x,p.y)); x.stroke();
    }else if(s.tool==='rect'){
      x.strokeRect(s.x1,s.y1,s.x2-s.x1,s.y2-s.y1);
    }else if(s.tool==='arrow'){
      x.beginPath(); x.moveTo(s.x1,s.y1); x.lineTo(s.x2,s.y2); x.stroke();
      const a=Math.atan2(s.y2-s.y1,s.x2-s.x1), h=lw*5;
      x.beginPath(); x.moveTo(s.x2,s.y2);
      x.lineTo(s.x2-h*Math.cos(a-0.45),s.y2-h*Math.sin(a-0.45));
      x.lineTo(s.x2-h*Math.cos(a+0.45),s.y2-h*Math.sin(a+0.45));
      x.closePath(); x.fill();
    }else if(s.tool==='text'){
      x.font=`${lw*5}px -apple-system,sans-serif`;
      x.fillText(s.txt,s.x,s.y);
    }
    if(s.n){
      const {bx,by}=badgeAnchor(s);
      const r=lw*3.2;
      x.beginPath();x.arc(bx,by-r*1.2,r,0,7);x.fillStyle=s.color;x.fill();
      x.fillStyle='#fff';x.font=`600 ${r*1.2}px -apple-system,sans-serif`;
      x.textAlign='center';x.textBaseline='middle';
      x.fillText(s.n,bx,by-r*1.2);
      x.textAlign='start';x.textBaseline='alphabetic';
    }
  }
}
function badgeAnchor(s){
  if(s.tool==='rect') return {bx:Math.min(s.x1,s.x2), by:Math.min(s.y1,s.y2)};
  if(s.tool==='arrow') return {bx:s.x1, by:s.y1};
  if(s.tool==='pen') return {bx:s.pts[0].x, by:s.pts[0].y};
  return {bx:s.x, by:s.y};
}
function badgeHit(p){
  const c=cv(), lw=Math.max(2,c.width/300), r=lw*3.2;
  for(const s of annotStrokes){
    if(!s.n) continue;
    const {bx,by}=badgeAnchor(s);
    if(Math.hypot(p.x-bx, p.y-(by-r*1.2)) <= r*1.6) return s;
  }
  return null;
}
function annotAskNote(stroke, clientX, clientY){
  const box=document.getElementById('annotNote'), inp=box.querySelector('input');
  const isEdit = !!stroke.note;
  if(!stroke.n) stroke.n=annotStrokes.filter(s=>s.n).length+1;
  box.querySelector('.nb').textContent=stroke.n;
  box.style.display='flex';
  box.style.left=Math.min(clientX, window.innerWidth-340)+'px';
  box.style.top=Math.min(clientY+14, window.innerHeight-60)+'px';
  inp.value=stroke.note||''; inp.focus(); inp.select();
  annotRedraw();
  const close=()=>{box.style.display='none'; annotRedraw();};
  box.querySelector('.del').onclick=()=>{
    const i=annotStrokes.indexOf(stroke);
    if(i>=0) annotStrokes.splice(i,1);
    annotRenumber(); annotSent=false; close();
  };
  inp.onkeydown=e=>{
    e.stopPropagation();
    if(e.key==='Enter'){
      stroke.note=inp.value.trim();
      if(!stroke.note){delete stroke.n; delete stroke.note; annotRenumber();}
      annotSent=false; close();
    }else if(e.key==='Escape'){
      if(!isEdit){delete stroke.n; annotRenumber();}
      close();
    }
  };
}
function annotRenumber(){
  let n=0;
  for(const s of annotStrokes){ if(s.note){s.n=++n;} else {delete s.n; delete s.note;} }
}
function annotInit(){
  const c=cv();
  c.addEventListener('pointerdown',e=>{
    const p=annotPos(e), col=document.getElementById('annotColor').value;
    const hit=badgeHit(p);
    if(hit){annotAskNote(hit, e.clientX, e.clientY); return;}
    annotCur=annotTool==='pen'?{tool:'pen',pts:[p],color:col}:{tool:annotTool,x1:p.x,y1:p.y,x2:p.x,y2:p.y,color:col};
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove',e=>{
    if(!annotCur)return;
    const p=annotPos(e);
    if(annotCur.tool==='pen')annotCur.pts.push(p);else{annotCur.x2=p.x;annotCur.y2=p.y;}
    annotRedraw();
  });
  c.addEventListener('pointerup',e=>{
    if(!annotCur)return;
    const s=annotCur; annotCur=null;
    const tiny = s.tool==='pen' ? s.pts.length<3 : Math.hypot(s.x2-s.x1,s.y2-s.y1)<4;
    if(tiny){annotRedraw(); return;}
    annotStrokes.push(s); annotSent=false; annotRedraw();
    annotAskNote(s,e.clientX,e.clientY);
  });
  document.querySelectorAll('#annotBar button[data-tool]').forEach(b=>{
    b.onclick=()=>{annotTool=b.dataset.tool;
      document.querySelectorAll('#annotBar button[data-tool]').forEach(o=>o.classList.toggle('sel',o===b));};
  });
  document.getElementById('annotUndo').onclick=()=>{annotStrokes.pop();annotRenumber();if(!annotStrokes.length)annotSent=true;document.getElementById('annotNote').style.display='none';annotRedraw();};
  document.getElementById('annotClear').onclick=()=>{annotStrokes=[];annotSent=true;document.getElementById('annotNote').style.display='none';annotRedraw();};
  document.getElementById('annotSend').onclick=async function(){
    const f=lbList[lbIdx], img=document.getElementById('lbImg');
    const scale=Math.min(1, 2200/img.naturalWidth);
    const out=document.createElement('canvas');
    out.width=Math.round(img.naturalWidth*scale); out.height=Math.round(img.naturalHeight*scale);
    const x=out.getContext('2d');
    x.drawImage(img,0,0,out.width,out.height); x.drawImage(cv(),0,0,out.width,out.height);
    this.textContent='\u23f3...';
    try{
      const r=await fetch('/save',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:f.name,dataURL:out.toDataURL('image/png'),notes:annotStrokes.filter(s=>s.note).map(s=>({n:s.n,text:s.note}))})});
      const j=await r.json();
      annotSent=true;
      this.textContent=j.sentToClaude?'Pasted to Claude \u2713':'Saved (path copied) \u2713';
    }catch(e){
      this.textContent='Server off \u2014 start the server';
    }
    setTimeout(()=>this.textContent='\u279c Claude',2500);
  };
}
annotInit();
function lbOpen(rel){const i=lbList.findIndex(f=>f.rel===rel);if(i>=0)lbShow(i);}
document.getElementById('lbClose').onclick=lbClose;
document.getElementById('lbPrev').onclick=e=>{e.stopPropagation();lbShow(lbIdx-1);};
document.getElementById('lbNext').onclick=e=>{e.stopPropagation();lbShow(lbIdx+1);};
lb().onclick=e=>{if(e.target.id==='lb')lbClose();};
document.getElementById('lbWrap').onclick=e=>e.stopPropagation();
document.getElementById('lbPdf').onclick=e=>e.stopPropagation();
render();

// --- live reload: auto-refresh when the gallery is rebuilt (Claude edits + rescans),
//     never interrupting an open viewer/lightbox or an active text selection ---
(function(){
  let boot = null;
  const getRev = () => fetch('/rev').then(r => r.json()).then(j => j.rev).catch(() => null);
  getRev().then(r => { boot = r; });
  setInterval(async () => {
    if(boot == null){ boot = await getRev(); return; }
    const lb = document.getElementById('lb');
    if(lb && lb.classList.contains('show')) return;        // a viewer/lightbox is open
    const sel = window.getSelection && window.getSelection();
    if(sel && String(sel).trim()) return;                  // user is selecting text
    const r = await getRev();
    if(r != null && r !== boot) location.reload();
  }, 2500);
})();
</script>
</body>
</html>"""


def prewarm_image_thumbs(rows, limit=400):
    """Pre-generate the server /thumb cache for the NEWEST images, in parallel, so
    the first paint after a (re)build is instant instead of generating them on
    demand at view time. Same key scheme as fig_annotate_server's /thumb endpoint
    (md5(realpath:int(mtime):480)). Incremental: only uncached images are built, so
    a rescan after adding a few plots warms only those."""
    if NO_THUMBS or sys.platform != "darwin":
        return
    imgs = sorted(((r["mtime"], os.path.join(ROOT, r["rel"])) for r in rows
                   if r["ext"] in ("png", "jpg", "jpeg")), reverse=True)[:limit]
    todo = []
    for mt, full in imgs:
        key = hashlib.md5((os.path.realpath(full) + ":" + str(int(mt)) + ":480").encode()).hexdigest()
        out = os.path.join(THUMB_DIR, "imgthumb_" + key + ".png")
        if not os.path.exists(out):
            todo.append((full, out))
    if not todo:
        return
    os.makedirs(THUMB_DIR, exist_ok=True)
    def gen(job):
        full, out = job
        try:
            subprocess.run(["sips", "-Z", "480", "-s", "format", "png", full, "--out", out],
                           capture_output=True, timeout=20, check=True)
        except Exception:
            pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
        list(ex.map(gen, todo))
    print(f"[gallery] pre-warmed {len(todo)} image thumbnail(s)")


def main():
    rows = scan()
    prewarm_image_thumbs(rows)
    folders = sorted({r["folder"] for r in rows})
    gen = time.strftime("%Y-%m-%d %H:%M")
    _esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    wordmark = _esc(os.environ.get("GALLERY_TITLE") or "Gallery")
    project = _esc(os.path.basename(ROOT.rstrip("/")) or "project")
    # __ROOT__ lands inside single-quoted JS string literals ('__ROOT__/'+rel);
    # escape it for that context so a path with a quote/backslash can't break the script.
    root_js = ROOT.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "").replace("</", "<\\/")
    html = (HTML
            .replace("__TITLE__", f"{wordmark} · {project}")
            .replace("__WORDMARK__", wordmark)
            .replace("__PROJECT__", project)
            .replace("__COUNT__", f"{len(rows):,}")
            .replace("__GEN__", gen)
            .replace("__VER__", str(int(time.time())))
            .replace("__DATA__", json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__FOLDERS__", json.dumps(folders, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__FAVS__", json.dumps(sorted(cmux_favorites()), ensure_ascii=False).replace("</", "<\\/"))
            .replace("__ROOT__", root_js))
    # regression guard: the page is ONE inline <script>; an unescaped </script> in
    # embedded data (snippet/name/path) would close it early and blank the whole gallery.
    # The </ -> <\/ escaping above prevents that — fail loud if it ever regresses.
    n_close = html.count("</script>")
    if n_close != 1:
        raise SystemExit(f"build_gallery: emitted page has {n_close} </script> tags (expected 1) — "
                         "data escaping is broken; aborting rather than ship a blank gallery")
    out = os.path.join(ROOT, SELF)
    with open(out, "w") as f:
        f.write(html)
    print(f"[{gen}] {len(rows)} files indexed -> {out}")


if __name__ == "__main__":
    main()
