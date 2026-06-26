"""Fullscreen contract for the gallery + SVG viewer.

LESSON (learned the hard way, twice): Orca's embedded WebKit ACCEPTS
`requestFullscreen()` — the pane fills the whole screen — but IGNORES
`document.exitFullscreen()` (and the webkit-prefixed variant). The pane then
stays stuck full-screen on exit. No client-side trick fixes it: not a reflow,
not calling both exit APIs, not requesting FS on a child element.

So inside Orca we use a server-assisted mode: the lightbox enters native
fullscreen, but exit calls a local-only server route that activates Orca and
sends the macOS fullscreen toggle through the Orca desktop bridge. Real browsers
keep plain ?nativeFs=1. Other embedded shells still default to CSS-only unless
they explicitly opt in.
"""

import unittest
import sys
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmux_gallery


class FullscreenRegressionTests(unittest.TestCase):
    def test_server_has_orca_fullscreen_exit_route(self):
        server = (ROOT / "fig_annotate_server.py").read_text()
        self.assertIn("/orca-fullscreen-exit", server)
        self.assertIn("def orca_fullscreen_exit()", server)
        self.assertIn("get-app-state", server)
        self.assertIn("list-windows", server)
        self.assertIn("Control+Command+F", server)

    def test_gallery_skips_native_fullscreen_in_embedded_shells(self):
        gallery = (ROOT / "build_gallery.py").read_text()

        self.assertIn("function lbNativeFsAllowed()", gallery)
        self.assertIn("function lbOrcaFsExitAllowed()", gallery)
        self.assertIn("/orca-fullscreen-exit", gallery)
        self.assertIn("p.get('orcaFs')==='1'", gallery)
        self.assertIn(r"\b(Orca|Electron|cmux)\b", gallery)
        guard = "if(!lbNativeFsAllowed()){nativeFsOk=false;return;}"
        native_call = "const req=root.requestFullscreen||root.webkitRequestFullscreen;"
        self.assertIn(guard, gallery)
        self.assertIn(native_call, gallery)
        self.assertLess(gallery.index(guard), gallery.index(native_call))

    def test_svg_viewer_skips_native_fullscreen_in_embedded_shells(self):
        viewer = (ROOT / "assets" / "svg_viewer.html").read_text()

        self.assertIn("function nativeFsAllowed()", viewer)
        self.assertIn(r"\b(Orca|Electron|cmux)\b", viewer)
        guard = "if(!nativeFsAllowed()) return;"
        native_call = (
            "const req=document.documentElement.requestFullscreen || "
            "document.documentElement.webkitRequestFullscreen;"
        )
        self.assertIn(guard, viewer)
        self.assertIn(native_call, viewer)
        self.assertLess(viewer.index(guard), viewer.index(native_call))
        self.assertIn("body.fs-mode header{display:none}", viewer)
        self.assertIn("if(window.self!==window.top) return false;", viewer)
        self.assertIn("return false;", viewer.split("function nativeFsAllowed()")[1])

    def test_gallery_url_selects_fullscreen_mode_from_shell(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                cmux_gallery.gallery_url(8790),
                "http://127.0.0.1:8790/figures_index.html?nativeFs=1",
            )

        with patch.dict("os.environ", {"ORCA_APP_VERSION": "1.4.101"}, clear=True):
            self.assertEqual(
                cmux_gallery.gallery_url(8790),
                "http://127.0.0.1:8790/figures_index.html?orcaFs=1",
            )

        with patch.dict("os.environ", {"TERM_PROGRAM": "Orca"}, clear=True):
            self.assertEqual(
                cmux_gallery.gallery_url(9000),
                "http://127.0.0.1:9000/figures_index.html?orcaFs=1",
            )

    def test_gallery_defaults_to_css_fullscreen_without_native_flag(self):
        gallery = (ROOT / "build_gallery.py").read_text()
        self.assertIn("return false;", gallery.split("function lbNativeFsAllowed()")[1])

    def test_fullscreen_image_fills_the_viewport(self):
        # The fullscreen image must fill the viewport (width/height:100vw/vh) so
        # it takes the whole screen in true fullscreen (cmux) and fills the pane
        # in CSS pane-fill (Orca). object-fit:contain keeps the aspect ratio.
        gallery = (ROOT / "build_gallery.py").read_text()
        fs_img = gallery.split("#lb.fs img{")[1].split("}")[0]
        self.assertIn("width:100vw;height:100vh", fs_img)
        self.assertIn("object-fit:contain", fs_img)


if __name__ == "__main__":
    unittest.main()
