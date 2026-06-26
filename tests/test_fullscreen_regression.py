"""Fullscreen contract for the gallery + SVG viewer.

LESSON (learned the hard way, twice): Orca's embedded WebKit ACCEPTS
`requestFullscreen()` — the pane fills the whole screen — but IGNORES
`document.exitFullscreen()` (and the webkit-prefixed variant). The pane then
stays stuck full-screen on exit. No client-side trick fixes it: not a reflow,
not calling both exit APIs, not requesting FS on a child element. An earlier
server-driven `/orca-window-fs` toggle was also tried and removed.

So inside Orca (and other embedded shells) we use CSS-only fullscreen: the
lightbox fills the *pane* and always exits cleanly. Real native whole-screen is
reachable only in a real browser (Safari/Chrome) via ?nativeFs=1. These tests
keep native FS disabled for embedded shells — do not "fix" them by flipping the
default; that re-introduces the stuck-pane bug.
"""

import unittest
import sys
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmux_gallery


class FullscreenRegressionTests(unittest.TestCase):
    def test_server_does_not_toggle_orca_window_fullscreen(self):
        server = (ROOT / "fig_annotate_server.py").read_text()
        self.assertNotIn("/orca-window-fs", server)
        self.assertNotIn("orca_window_fs_toggle", server)

    def test_gallery_skips_native_fullscreen_in_embedded_shells(self):
        gallery = (ROOT / "build_gallery.py").read_text()

        self.assertIn("function lbNativeFsAllowed()", gallery)
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
                "http://127.0.0.1:8790/figures_index.html?cssFs=1",
            )

        with patch.dict("os.environ", {"TERM_PROGRAM": "Orca"}, clear=True):
            self.assertEqual(
                cmux_gallery.gallery_url(9000),
                "http://127.0.0.1:9000/figures_index.html?cssFs=1",
            )

    def test_gallery_defaults_to_css_fullscreen_without_native_flag(self):
        gallery = (ROOT / "build_gallery.py").read_text()
        self.assertIn("return false;", gallery.split("function lbNativeFsAllowed()")[1])

    def test_fullscreen_image_is_not_upscaled(self):
        # The fullscreen image must NOT be force-stretched to the viewport
        # (width:100vw;height:100vh) — that upscaled small figures into a blur.
        gallery = (ROOT / "build_gallery.py").read_text()
        fs_img = gallery.split("#lb.fs img{")[1].split("}")[0]
        self.assertIn("max-width:100vw", fs_img)
        self.assertIn("max-height:100vh", fs_img)
        self.assertNotIn("width:100vw;height:100vh", fs_img)


if __name__ == "__main__":
    unittest.main()
