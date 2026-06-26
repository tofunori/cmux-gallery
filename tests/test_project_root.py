import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmux_gallery


class ProjectRootTests(unittest.TestCase):
    def test_default_project_root_falls_back_to_start_directory_outside_git(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(cmux_gallery, "git_project_root", return_value=None):
                self.assertEqual(cmux_gallery.default_project_root(td), os.path.abspath(td))

    def test_default_project_root_uses_enclosing_git_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            nested = os.path.join(td, "figures", "plots")
            os.makedirs(nested)
            with patch.object(cmux_gallery, "git_project_root", return_value=os.path.abspath(td)):
                self.assertEqual(cmux_gallery.default_project_root(nested), os.path.abspath(td))

    def test_git_project_root_returns_git_toplevel(self):
        root = os.path.abspath("/tmp/example-project")
        result = SimpleNamespace(returncode=0, stdout=root + "\n")
        with patch.object(subprocess, "run", return_value=result) as run:
            self.assertEqual(cmux_gallery.git_project_root("/tmp/example-project/subdir"), root)
            run.assert_called_once_with(
                ["git", "-C", "/tmp/example-project/subdir", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=2,
            )

    def test_root_arg_expands_user_path(self):
        self.assertEqual(cmux_gallery.root_arg("~/example"), os.path.expanduser("~/example"))


if __name__ == "__main__":
    unittest.main()
