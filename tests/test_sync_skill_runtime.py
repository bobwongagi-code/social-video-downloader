import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_skill_runtime.py"


class SkillRuntimeSyncTests(unittest.TestCase):
    def test_install_then_check_matches_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_root = Path(temporary) / "codex-home"
            install = subprocess.run(
                [sys.executable, str(SCRIPT), "--install", "--target-root", str(target_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            check = subprocess.run(
                [sys.executable, str(SCRIPT), "--check", "--target-root", str(target_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertTrue(
                (target_root / "skills" / "social-video-downloader" / "scripts" / "download_workflow.py").is_file()
            )

    def test_check_rejects_stale_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_root = Path(temporary) / "codex-home"
            install = subprocess.run(
                [sys.executable, str(SCRIPT), "--install", "--target-root", str(target_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            installed_script = (
                target_root / "skills" / "social-video-downloader" / "scripts" / "constants.py"
            )
            installed_script.write_text(installed_script.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            check = subprocess.run(
                [sys.executable, str(SCRIPT), "--check", "--target-root", str(target_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 1)
            self.assertIn("stale: scripts/constants.py", check.stderr)


if __name__ == "__main__":
    unittest.main()
