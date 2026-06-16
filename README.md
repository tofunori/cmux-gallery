# cmux-gallery

A portable artifact gallery + annotation tool for [cmux](https://github.com/manaflow-ai/cmux).
Point it at any project and it builds a searchable HTML gallery of your figures,
PDFs, data and code — with thumbnails, an image lightbox, PDF / Markdown / code
viewers, and figure annotation. Clicking a card opens the source file in a cmux
pane. No manual setup per project.

It generalises a figures-index builder + a small local server so they work in
**any** project root.

## Features

Search · sort · folder + format filters · archive toggle · favourites + star
ratings · Quick-Look thumbnails (macOS) · image lightbox · embedded PDF /
Markdown / code viewers · figure annotation (pen / arrow / rect + notes).

## How it works

`run` builds the gallery, provisions the viewer assets into the project, starts a
local server (on a stable port, with the project as its root) and opens it as a
cmux browser surface. Keep the launching terminal/pane open — it hosts the
server; Ctrl-C stops it.

```
build  → GALLERY_ROOT=<root> build_gallery.py  +  copy viewer assets
serve  → fig_annotate_server.py on a free port, project as root
open   → cmux browser open http://127.0.0.1:<port>/figures_index.html
```

## Install

```bash
git clone https://github.com/tofunori/cmux-gallery.git ~/tools/cmux-gallery
bash ~/tools/cmux-gallery/install.sh
```

`install.sh` links `cmux-gallery` into `~/.local/bin`, checks it is on your `PATH`
(and shows how to add it if not), and verifies `python3` + `cmux`. Manual
equivalent: `ln -s …/cmux_gallery.py ~/.local/bin/cmux-gallery && chmod +x …`.

`build` needs only the Python 3 standard library; `run`/`serve` need the `cmux`
CLI. Thumbnails use macOS `qlmanage` (skipped gracefully elsewhere).

## Use

```bash
cmux-gallery run                 # build + serve + open in cmux (keep the pane open)
cmux-gallery serve               # build + HOST the server, self-healing, no browser tab
cmux-gallery run --root /path    # a specific project (default: current dir)
cmux-gallery build               # just write the HTML + viewers (no server)
```

Each project gets a **stable port** derived from its path (8790–9789), so the URL
is the same every time — open it in any browser (cmux or system) and bookmark it,
e.g. `http://127.0.0.1:8790/figures_index.html`. Pin one with `--port <n>`.

### As a cmux command / Dock control

- **Command Palette / + menu**: copy the `actions` + `commands` from
  [`cmux.example.json`](./cmux.example.json) into `~/.config/cmux/cmux.json`,
  then run **Project Gallery**.
- **Dock** (recommended): copy [`dock.example.json`](./dock.example.json) into the
  project's `.cmux/dock.json`. It runs `cmux-gallery serve`, which **hosts** the
  server, restarts it if it dies, and auto-starts when cmux launches.

## Keeping it running

The server lives only as long as its host process. Pick one:

- **A cmux Dock control or pane (recommended).** `cmux-gallery serve` hosts the
  server and self-heals; in the Dock it also auto-starts with cmux. Because it
  runs *inside cmux* it inherits cmux's file access — which matters on macOS:

  > **Don't use a launchd LaunchAgent for a project under `~/Documents`,
  > `~/Desktop`, `~/Downloads` or iCloud Drive.** macOS **TCC** blocks background
  > launchd processes from reading those folders, so an "always-on" agent there
  > starts but returns **404 for every file** (it binds the socket but can't read
  > your files) unless you grant its `python3` **Full Disk Access**. The
  > cmux-hosted server avoids this entirely.

- **A plain terminal:** `cmux-gallery run` (or `serve`) in a pane you keep open.

To run it even when cmux is closed: move the project outside those protected
folders, or grant Full Disk Access to your `python3` and launch `cmux-gallery
serve` from a LaunchAgent.

## Configuration

| flag / env | meaning |
|---|---|
| `--root <dir>` | project to scan (default: current dir) |
| `--port <n>` | server port (default: a stable per-project port 8790–9789; 0 = random) |
| `GALLERY_TITLE` | header wordmark (default `Gallery`) |
| `GALLERY_NO_THUMBS=1` | skip Quick-Look thumbnail generation |

## Notes & caveats

- **Untrusted filenames are safe**: filenames are HTML-escaped and all card
  handlers use `data-*` delegation, so a crafted filename can't execute script.
- **annotate → Claude** and the LaTeX/`open`/trash actions are macOS- and
  Claude-Code-in-cmux-specific; they degrade gracefully elsewhere.
- `figures_index.html`, `.fig_thumbs/`, `annotations/` are regenerated per build
  (gitignored).

## Bundled third-party

Vendored under `assets/`, each under its own license (see [LICENSE](./LICENSE)):

- [pdf.js](https://github.com/mozilla/pdf.js) (Apache-2.0) — see `assets/pdfjs/NOTICE`
- [CodeMirror 5](https://codemirror.net) (MIT) — `assets/cm/`
- [marked](https://marked.js.org) (MIT) — `assets/marked.min.js`
- [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 / MPL-2.0) — `assets/purify.min.js`

## License

MIT — see [LICENSE](./LICENSE).
