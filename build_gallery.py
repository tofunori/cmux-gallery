#!/usr/bin/env python3
"""Regenerate figures_index.html — an interactive gallery of every figure in the project.

Usage:
    python build_figures_index.py

Scans the project for image files (png, pdf, svg, jpg, html), collects metadata,
and writes a self-contained figures_index.html at the project root.
Run it again any time to refresh the index after producing new figures.
"""
import os, json, time, hashlib, subprocess

ROOT = os.path.abspath(os.environ.get("GALLERY_ROOT") or os.getcwd())
EXTS = {".png", ".pdf", ".html", ".docx", ".xlsx", ".xls", ".csv", ".md", ".py", ".r", ".jl", ".tex", ".sh"}
# Skip these directories entirely (virtualenvs, git, caches, worktrees, the index itself)
EXCLUDE_PARTS = {".git", ".venv", ".venv-era5", ".venv-codex", "node_modules",
                 "__pycache__", ".ipynb_checkpoints", "worktrees", ".claude", ".fig_thumbs"}
ARCHIVE_HINTS = ("_archive", "menage_", "/tmp/", "tmp_dir", "/tmp", "raqdps_tests")
SELF = "figures_index.html"
SNIP_EXTS = (".py", ".r", ".jl", ".sh", ".tex", ".md", ".csv")


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

def thumb_key(rel, mtime):
    return hashlib.md5(f"{rel}:{mtime}".encode()).hexdigest()


def build_thumbs(pending):
    """Generate missing thumbnails in batches (one qlmanage call per batch).

    pending: list of (full, key). qlmanage writes <basename>.png, so duplicate
    basenames are spread across different batches."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    batches = []
    for full, key in pending:
        base = os.path.basename(full)
        for b in batches:
            if base not in b:
                b[base] = (full, key)
                break
        else:
            batches.append({base: (full, key)})
    for b in batches:
        files = [full for full, _ in b.values()]
        for i in range(0, len(files), 100):
            chunk = files[i:i+100]
            try:
                subprocess.run(["qlmanage", "-t", "-s", "480", "-o", THUMB_DIR] + chunk,
                               capture_output=True, timeout=30 + 5 * len(chunk))
            except Exception:
                pass
        for base, (full, key) in b.items():
            produced = os.path.join(THUMB_DIR, base + ".png")
            out = os.path.join(THUMB_DIR, key + ".png")
            if os.path.exists(produced):
                os.replace(produced, out)
            else:
                open(os.path.join(THUMB_DIR, key + ".fail"), "w").close()


def scan():
    rows = []
    thumb_pending = []
    keys_seen = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if set(dirpath.split(os.sep)) & EXCLUDE_PARTS:
            dirnames[:] = []
            continue
        for fn in filenames:
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
            if ext in (".pdf", ".docx", ".xlsx", ".xls"):
                key = thumb_key(rel, int(st.st_mtime))
                keys_seen.add(key)
                if os.path.exists(os.path.join(THUMB_DIR, key + ".png")):
                    thumb = ".fig_thumbs/" + key + ".png"
                elif not os.path.exists(os.path.join(THUMB_DIR, key + ".fail")):
                    thumb_pending.append((full, key))
                    thumb = ".fig_thumbs/" + key + ".png"
            rows.append({
                "thumb": thumb,
                "snippet": read_snippet(full) if ext in SNIP_EXTS else None,
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
  :root{ --bg:#18181b; --card:#27272a; --card2:#1f1f23; --txt:#e4e4e7; --muted:#a1a1aa;
         --accent:#5b9dff; --arch:#3a2f1a; --border:#3f3f46; }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--txt);font-size:14px}
  header{position:sticky;top:0;z-index:10;background:rgba(24,24,27,.97);backdrop-filter:blur(8px);
         border-bottom:1px solid var(--border);padding:14px 20px}
  .brand{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .brand .logo{font-size:15px;color:var(--accent);align-self:center}
  .brand .wm{font-size:15px;font-weight:600;letter-spacing:.01em}
  .brand .proj{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .brand .stat{margin-left:auto;font-size:12px;color:var(--muted)}
  .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  input[type=search]{flex:1;min-width:240px;padding:9px 12px;border-radius:8px;border:1px solid var(--border);
        background:var(--card);color:var(--txt);font-size:14px}
  select,button{padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--card);
        color:var(--txt);font-size:13px;cursor:pointer}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:20px;
        border:1px solid var(--border);background:var(--card);cursor:pointer;user-select:none;font-size:12px}
  .chip.off{opacity:.4}
  #fmtMenu{position:absolute;z-index:50;display:none;flex-direction:column;gap:2px;margin-top:6px;
      background:#27272a;border:1px solid #3f3f46;border-radius:10px;padding:8px;min-width:140px;
      box-shadow:0 8px 28px rgba(0,0,0,.5)}
  #fmtMenu label{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;
      font-size:12.5px;cursor:pointer;user-select:none}
  #fmtMenu label:hover{background:rgba(255,255,255,.05)}
  #fmtMenu input{accent-color:var(--accent)}
  .count{color:var(--muted);font-size:12px;margin-left:auto}
  main{padding:18px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;
        display:flex;flex-direction:column;transition:.12s}
  .card:hover{border-color:var(--accent);transform:translateY(-2px)}
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
  .acts{display:flex;gap:6px;padding:0 12px 12px}
  .acts a,.acts button{flex:1;text-align:center;text-decoration:none;font-size:12px;padding:6px 4px;
        background:transparent;border:1px solid #3a3f4a;border-radius:7px;color:#c9cfda;cursor:pointer;transition:.12s}
  .acts a:hover,.acts button:hover{border-color:#5b6575;color:#fff;background:rgba(255,255,255,.04)}
  .selbox{position:absolute;top:6px;left:6px;font-size:15px;cursor:pointer;line-height:1;
        background:rgba(15,17,21,.85);border:1px solid #3a3f4a;border-radius:6px;padding:4px 7px;user-select:none;color:#e6e8ec}
  .selbox.on{color:#ff6b6b;border-color:#ff6b6b}
  .star{position:absolute;top:6px;right:6px;font-size:18px;cursor:pointer;line-height:1;
        background:rgba(15,17,21,.85);border:1px solid #3a3f4a;border-radius:50%;padding:5px 6px;user-select:none;color:#e6e8ec}
  .star.on{color:#ffce3a;border-color:#ffce3a}
  .rate{display:flex;gap:1px;margin-top:3px;font-size:13px;line-height:1;user-select:none}
  .rate span{cursor:pointer;color:#3a3f4a;transition:color .1s}
  .rate span.on{color:#ffce3a}
  .rate span:hover{color:#ffe28a}
  .card{position:relative}
  .empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px}
  #lb{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;
      flex-direction:column;align-items:center;justify-content:center;cursor:zoom-out}
  #lb.show{display:flex}
  #lb img{max-width:94vw;max-height:86vh;object-fit:contain;background:#fff;border-radius:6px;cursor:zoom-in}
  #lb.fs img{max-width:100vw;max-height:100vh;border-radius:0}
  #lb.fs #lbCap,#lb.fs .lbBtn,#lb.fs #lbClose{display:none}
  #lb.vw{justify-content:flex-start}
  #lb.vw #lbPdf{width:100vw !important;height:100vh !important;border-radius:0}
  #lb.vw #lbCap,#lb.vw .lbBtn,#lb.vw #lbFs{display:none}
  #lbFs{position:fixed;top:12px;right:58px;font-size:20px;color:#bbb;cursor:pointer;z-index:101}
  #lbFs:hover{color:#fff}
  #lbCap{color:#ddd;font-size:13px;margin-top:10px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;justify-content:center;max-width:94vw;text-align:center}
  #lbCap span{word-break:break-all}
  #lbCap a{color:var(--accent)}
  .lbBtn{position:fixed;top:50%;transform:translateY(-50%);font-size:34px;color:#bbb;cursor:pointer;
         padding:18px 14px;user-select:none;z-index:101}
  .lbBtn:hover{color:#fff}
  #lbPrev{left:6px} #lbNext{right:6px}
  #annotBar{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:102;display:none;
            gap:6px;align-items:center;background:rgba(20,23,30,.95);border:1px solid var(--border);
            border-radius:10px;padding:6px 10px}
  #lb.annot #annotBar{display:flex}
  #annotBar button{padding:5px 9px;font-size:13px}
  #annotBar button.sel{background:var(--accent);color:#fff}
  #annotBar input[type=color]{width:30px;height:28px;border:none;background:none;cursor:pointer;padding:0}
  #lbWrap{position:relative;cursor:default}
  #annotCv{position:absolute;inset:0;display:none;touch-action:none}
  #annotNote{position:fixed;z-index:103;display:none;align-items:center;gap:8px;
      background:#2a2e38;border:1px solid #3a3f4a;border-radius:22px;padding:7px 14px;
      box-shadow:0 6px 24px rgba(0,0,0,.5)}
  #annotNote input{background:none;border:none;outline:none;color:#e6e8ec;font-size:13px;width:240px}
  #annotNote .del{cursor:pointer;color:#9aa3b2;font-size:15px;padding:0 2px}
  #annotNote .del:hover{color:#ff6b6b}
  #annotNote .nb{background:var(--accent);color:#fff;border-radius:50%;width:22px;height:22px;
      display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex:none}
  #lb.annot #annotCv{display:block;cursor:crosshair}
  #lbClose{position:fixed;top:12px;right:18px;font-size:26px;color:#bbb;cursor:pointer;z-index:101}
  footer{padding:20px;text-align:center;color:var(--muted);font-size:11px}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="logo">◫</span>
    <span class="wm">__WORDMARK__</span>
    <span class="proj">__PROJECT__</span>
    <span class="stat">__COUNT__ files · __GEN__</span>
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search by name or folder… (e.g. trend, map, results)">
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
    <span class="chip" data-ext="png">PNG</span>
    <span class="chip" data-ext="pdf">PDF</span>
    <span class="chip" id="fmtChip">Formats &#9662;</span>
    <div id="fmtMenu"></div>
    <span class="chip on" id="archChip">Include archives</span>
    <span class="chip off" id="favChip">&#9733; Favorites</span>
    <span id="rateFilter" style="display:none"></span>
    <button id="favExport" title="Copy fav commands to sync with the cmux Dock">Sync favs &#8594; clipboard</button>
    <button id="quoteClear" style="display:none" title="Clear the annotation pending in the Claude statusline">&#9998;&#10005; Annotation</button>
    <button id="rescan" title="Re-run build_figures_index.py and reload">&#8635; Rescan</button>
    <button id="delSel" style="display:none;background:#5c1f1f;border-color:#7a2a2a">&#128465; Delete (0)</button>
    <span class="count" id="count"></span>
  </div>
</header>
<main id="grid"></main>
<div id="lb">
  <span id="lbClose">&#10005;</span>
  <span id="lbFs" title="Fullscreen (f or double-click)">&#9974;</span>
  <span class="lbBtn" id="lbPrev">&#8249;</span>
  <span class="lbBtn" id="lbNext">&#8250;</span>
  <div id="annotBar">
    <button data-tool="arrow" title="Arrow (1)">&#8594;</button>
    <button data-tool="rect" class="sel" title="Rectangle (2)">&#9645;</button>
    <input type="color" id="annotColor" value="#ff2d2d" title="Color">
    <button id="annotUndo" title="Undo">&#8630;</button>
    <button id="annotClear" title="Clear all">&#10006;</button>
    <button id="annotSend" title="Save the annotated PNG and paste the path into Claude Code" style="background:var(--accent);color:#fff">&#10148; Claude</button>
  </div>
  <div id="lbWrap"><img id="lbImg" src="" alt=""><canvas id="annotCv"></canvas></div>
  <div id="annotNote"><span class="nb">1</span><input type="text" placeholder="Add a comment... (Enter)"><span class="del" title="Delete this annotation">&#128465;</span></div>
  <iframe id="lbPdf" style="display:none;width:94vw;height:86vh;border:none;border-radius:6px;background:#fff"></iframe>
  <div id="lbCap"></div>
</div>
<footer>Double-click a thumbnail or "Open" to view the file. This file must stay at the project root for the links to work. Re-run build_figures_index.py to refresh.</footer>
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
  else if(act==='sel') toggleSel(rel, el);
  else if(act==='lb') lbOpen(rel);
  else if(act==='open') openDefault(rel);
  else if(act==='rate') setRate(rel, +el.dataset.n, e);
  else if(act==='copy'){ navigator.clipboard.writeText(rel); el.textContent='✓'; setTimeout(()=>el.textContent='Path',1200); }
});
const SEED_FAVS = __FAVS__;
let favs = new Set(JSON.parse(localStorage.getItem('figFavs')||'[]'));
SEED_FAVS.forEach(f=>favs.add(f));
const saveFavs = ()=>localStorage.setItem('figFavs', JSON.stringify([...favs]));
saveFavs();
let ratings = JSON.parse(localStorage.getItem('figRatings')||'{}');
let stateTimer=null;
function pushState(){
  clearTimeout(stateTimer);
  stateTimer=setTimeout(()=>{
    fetch('/state',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({favs:[...favs],ratings})}).catch(()=>{});
  },400);
}
const saveRatings = ()=>{localStorage.setItem('figRatings', JSON.stringify(ratings));pushState();};
fetch('/state').then(r=>r.json()).then(st=>{
  (st.favs||[]).forEach(f=>favs.add(f));
  Object.assign(ratings, st.ratings||{});
  localStorage.setItem('figFavs', JSON.stringify([...favs]));
  localStorage.setItem('figRatings', JSON.stringify(ratings));
  document.getElementById('favChip').textContent='\u2605 Favorites ('+favs.size+')';
  render();
}).catch(()=>{});
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
function updateDelBtn(){
  const b = document.getElementById('delSel');
  b.style.display = selSet.size ? '' : 'none';
  b.textContent = '🗑 Delete (' + selSet.size + ')';
}
function toggleSel(rel, el){
  if(selSet.has(rel)){ selSet.delete(rel); el.classList.remove('on'); el.textContent='\u25A2'; }
  else{ selSet.add(rel); el.classList.add('on'); el.textContent='\u25A0'; }
  updateDelBtn();
}
function openDefault(rel){
  fetch('/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rel})});
}
let lbList = [], lbIdx = -1;
const lb=()=>document.getElementById('lb');
function lbShow(i){
  if(lbIdx>=0 && i!==lbIdx && !annotGuard()) return;
  if(i<0||i>=lbList.length) return;
  lbIdx=i; const f=lbList[i]; lb().classList.remove('annot');
  const isPdf=f.ext==='pdf', isMd=f.ext==='md', isCode=codeExt(f.ext);
  const img=document.getElementById('lbImg'), pdf=document.getElementById('lbPdf');
  img.style.display=(isPdf||isMd||isCode)?'none':'';
  pdf.style.display=(isPdf||isMd||isCode)?'':'none';
  lb().classList.toggle('vw', isPdf||isMd||isCode);  // full-window editor/viewer
  if(isPdf){pdf.src='/.fig_thumbs/pdf_viewer.html?file='+encodeURIComponent(f.rel)+'&v=__VER__';img.src='';}
  else if(isMd){pdf.src='/.fig_thumbs/md_viewer.html?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&file='+encodeURIComponent(f.rel)+'&v=__VER__';img.src='';}
  else if(isCode){pdf.src='/.fig_thumbs/code_editor.html?path='+encodeURIComponent('__ROOT__/'+f.rel)+'&v=__VER__';img.src='';}
  else{img.src=f.rel+'?v='+f.mtime;pdf.src='';}
  document.getElementById('lbCap').innerHTML=
    `<b>${esc(f.name)}</b><span>${esc(f.folder)}</span><span>${esc(f.mdate)}</span><a href="${escA(f.rel)}" target="_blank">open original</a>`+
    (imgExt(f.ext)?` <button onclick="annotToggle()" style="margin-left:8px">&#9998; Annotate</button>`:'');
  lb().classList.add('show');
}
function lbClose(){if(!annotGuard())return;lb().classList.remove('show');lb().classList.remove('annot');lb().classList.remove('fs');lbIdx=-1;}
function lbFsToggle(){
  const el=lb();
  if(document.fullscreenElement){document.exitFullscreen();el.classList.remove('fs');return;}
  if(el.requestFullscreen){el.requestFullscreen().then(()=>el.classList.add('fs')).catch(()=>el.classList.toggle('fs'));}
  else if(el.webkitRequestFullscreen){el.webkitRequestFullscreen();el.classList.add('fs');}
  else el.classList.toggle('fs');
}
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement)lb().classList.remove('fs');});
document.getElementById('lbFs').onclick=e=>{e.stopPropagation();lbFsToggle();};
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
  if(e.key==='Escape'){if(lb().classList.contains('fs')){lb().classList.remove('fs');return;}lbClose();}
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
const DEFAULT_EXTS = {png:true,pdf:false,html:false,docx:false,xlsx:false,xls:false,csv:false,md:false,py:false,r:false,jl:false,tex:false,sh:false};
const exts = Object.assign({}, DEFAULT_EXTS, JSON.parse(localStorage.getItem('figExts')||'{}'));
const saveExts = ()=>localStorage.setItem('figExts', JSON.stringify(exts));
let showArch = true;
const fmtSize = b => b>1048576?(b/1048576).toFixed(1)+' MB':b>1024?(b/1024).toFixed(0)+' KB':b+' B';
const imgExt = e => e==='png'||e==='jpg'||e==='jpeg'||e==='svg';
const appExt = e => e==='docx'||e==='xlsx'||e==='xls'||e==='csv';
const codeExt = e => e==='py'||e==='r'||e==='jl'||e==='tex'||e==='sh';
const FMT_LIST = [['html','HTML'],['docx','DOCX'],['xlsx','XLSX'],['csv','CSV'],['md','Markdown'],['py','Python'],['r','R'],['jl','Julia'],['tex','LaTeX'],['sh','Shell']];
const fmtMenu=document.getElementById('fmtMenu'), fmtChip=document.getElementById('fmtChip');
function fmtChipLabel(){
  const n=FMT_LIST.filter(([e])=>exts[e]).length;
  fmtChip.innerHTML='Formats'+(n?' ('+n+')':'')+' &#9662;';
  fmtChip.classList.toggle('off',!n);
}
fmtMenu.innerHTML=FMT_LIST.map(([e,lab])=>
  `<label><input type="checkbox" data-fmt="${e}" ${exts[e]?'checked':''}> ${lab}</label>`).join('');
fmtMenu.querySelectorAll('input').forEach(cb=>{
  cb.onchange=()=>{const e=cb.dataset.fmt;exts[e]=cb.checked;if(e==='xlsx')exts['xls']=cb.checked;saveExts();fmtChipLabel();render();};
});
fmtChip.onclick=e=>{
  e.stopPropagation();
  const r=fmtChip.getBoundingClientRect();
  fmtMenu.style.left=r.left+'px'; fmtMenu.style.top=(r.bottom+window.scrollY)+'px';
  fmtMenu.style.display=fmtMenu.style.display==='flex'?'none':'flex';
};
fmtMenu.onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{fmtMenu.style.display='none';});
fmtChipLabel();

const fsel = document.getElementById('folder');
FOLDERS.forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f;fsel.appendChild(o);});

function render(){
  const q = document.getElementById('q').value.toLowerCase().trim();
  const terms = q.split(/\\s+/).filter(Boolean);
  const sort = document.getElementById('sort').value;
  const fld = fsel.value;
  let list = FILES.filter(f=>{
    if(!exts[f.ext]) return false;
    if(!showArch && f.archive) return false;
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
  document.getElementById('count').textContent = list.length+' / '+FILES.length+' figures';
  lbList = list.filter(f=>imgExt(f.ext)||f.ext==='pdf'||f.ext==='md'||codeExt(f.ext));
  const grid=document.getElementById('grid');
  if(!list.length){grid.innerHTML='<div class="empty">No matching files.</div>';return;}
  const MAX=600;
  const slice=list.slice(0,MAX);
  grid.innerHTML = slice.map(f=>{
    const tsrc = imgExt(f.ext) ? f.rel+'?v='+f.mtime : (f.thumb||null);
    const thumb = f.snippet
      ? `<div class="snip">${esc(f.snippet)}</div>`
      : tsrc
      ? `<div class="thumb"><img loading="lazy" src="${escA(tsrc)}" alt=""></div>`
      : `<div class="ph"><span class="ext">${esc(f.ext.toUpperCase())}</span><span style="font-size:11px">no preview</span></div>`;
    const arch = f.archive?`<span class="tag archive">archive</span>`:'';
    const isFav = favs.has(f.rel);
    return `<div class="card ${f.archive?'arch':''}">
      <span class="star ${isFav?'on':''}" data-act="fav" data-rel="${escA(f.rel)}">${isFav?'\u2605':'\u2606'}</span>
      <span class="selbox ${selSet.has(f.rel)?'on':''}" data-act="sel" data-rel="${escA(f.rel)}">${selSet.has(f.rel)?'\u25A0':'\u25A2'}</span>
      ${(imgExt(f.ext)||f.ext==='pdf'||f.ext==='md'||codeExt(f.ext))?`<div data-act="lb" data-rel="${escA(f.rel)}" style="cursor:zoom-in">${thumb}</div>`:appExt(f.ext)?`<div data-act="open" data-rel="${escA(f.rel)}" style="cursor:pointer" title="Open with default app">${thumb}</div>`:`<a href="${escA(f.rel)}" target="_blank" style="text-decoration:none">${thumb}</a>`}
      <div class="meta">
        <div class="nm">${esc(f.name)}</div>
        ${isFav?rateRow(f.rel):''}
        <div class="fld">${esc(f.folder)}</div>
        <div class="row"><span class="tag">${esc(f.ext)}</span>${arch}<span title="created ${escA(f.bdate)} \u00b7 modified ${escA(f.mdate)}">${sort.startsWith('btime')?esc(f.bdate):esc(f.mdate)}</span><span>${fmtSize(f.size)}</span></div>
      </div>
      <div class="acts">
        <button data-act="open" data-rel="${escA(f.rel)}" title="Open with default app">Open</button>
        <button data-act="copy" data-rel="${escA(f.rel)}">Path</button>
      </div>
    </div>`;
  }).join('') + (list.length>MAX?`<div class="empty">… and ${list.length-MAX} more. Refine your search to see them.</div>`:'');
}
document.querySelectorAll('.chip[data-ext]').forEach(c=>{
  const e=c.dataset.ext;
  c.classList.toggle('off',!exts[e]);
  c.classList.toggle('on',!!exts[e]);
  c.onclick=()=>{exts[e]=!exts[e];if(e==='jpg')exts['jpeg']=exts['jpg'];if(e==='xlsx')exts['xls']=exts['xlsx'];c.classList.toggle('off',!exts[e]);c.classList.toggle('on',!!exts[e]);saveExts();render();};
});
document.getElementById('archChip').onclick=function(){showArch=!showArch;this.classList.toggle('off',!showArch);this.textContent=showArch?'Include archives':'Archives hidden';render();};
const favChip=document.getElementById('favChip');
favChip.textContent='\u2605 Favorites ('+favs.size+')';
const rateFilter=document.getElementById('rateFilter');
rateFilter.innerHTML=[1,2,3,4,5].map(n=>`<span class="chip off rf" data-n="${n}" title="Show only ${n}-star items">${'\u2605'.repeat(n)}</span>`).join('');
rateFilter.querySelectorAll('.rf').forEach(c=>{
  c.onclick=()=>{
    const n=+c.dataset.n;
    rateMin = rateMin===n ? 0 : n;
    rateFilter.querySelectorAll('.rf').forEach(x=>{const on=+x.dataset.n===rateMin;x.classList.toggle('on',on);x.classList.toggle('off',!on);});
    render();
  };
});
favChip.onclick=()=>{onlyFavs=!onlyFavs;favChip.classList.toggle('off',!onlyFavs);favChip.classList.toggle('on',onlyFavs);rateFilter.style.display=onlyFavs?'inline-flex':'none';if(!onlyFavs){rateMin=0;rateFilter.querySelectorAll('.rf').forEach(x=>{x.classList.remove('on');x.classList.add('off');});}render();};
document.getElementById('favExport').onclick=function(){
  const root='__ROOT__';
  const cmds=[...favs].map(r=>`fav "${root}/${r}"`).join('\\n');
  navigator.clipboard.writeText(cmds||'# no favorites');
  this.textContent='Copied \u2713 ('+favs.size+')';
  setTimeout(()=>this.textContent='Sync favs \u2192 clipboard',1500);
};
const quoteBtn=document.getElementById('quoteClear');
function quoteCheck(){fetch('/quote').then(r=>r.json()).then(j=>{quoteBtn.style.display=j.pending?'':'none';}).catch(()=>{});}
quoteCheck(); setInterval(quoteCheck, 30000);
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
  if(!confirm(selSet.size+' file(s) \u2192 trash?')) return;
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
// ---- Annotation ----
let annotTool='rect', annotStrokes=[], annotCur=null, annotSent=true;
function annotGuard(){
  if(lb().classList.contains('annot') && annotStrokes.length && !annotSent)
    return confirm('Unsaved annotations \u2014 discard them?');
  return true;
}
const cv=()=>document.getElementById('annotCv');
function annotToggle(){
  if(lb().classList.contains('annot') && !annotGuard()) return;
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
</script>
</body>
</html>"""


def main():
    rows = scan()
    folders = sorted({r["folder"] for r in rows})
    gen = time.strftime("%Y-%m-%d %H:%M")
    _esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    wordmark = _esc(os.environ.get("GALLERY_TITLE") or "Gallery")
    project = _esc(os.path.basename(ROOT.rstrip("/")) or "project")
    html = (HTML
            .replace("__TITLE__", f"{wordmark} · {project}")
            .replace("__WORDMARK__", wordmark)
            .replace("__PROJECT__", project)
            .replace("__COUNT__", f"{len(rows):,}")
            .replace("__GEN__", gen)
            .replace("__VER__", str(int(time.time())))
            .replace("__DATA__", json.dumps(rows, ensure_ascii=False))
            .replace("__FOLDERS__", json.dumps(folders, ensure_ascii=False))
            .replace("__FAVS__", json.dumps(sorted(cmux_favorites()), ensure_ascii=False))
            .replace("__ROOT__", ROOT))
    out = os.path.join(ROOT, SELF)
    with open(out, "w") as f:
        f.write(html)
    print(f"[{gen}] {len(rows)} files indexed -> {out}")


if __name__ == "__main__":
    main()
