import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import media_probe


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "compress_for_whatsapp.sh"


class CompressForWhatsAppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg and FFprobe are required for compression integration tests")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="social-video-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_ffmpeg(self, *args: str) -> None:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def make_source(
        self,
        name: str,
        *,
        audio: bool = True,
        rotate: bool = False,
        non_square_pixels: bool = False,
    ) -> Path:
        output = self.root / name
        command = [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=24",
        ]
        if audio:
            command.extend(["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"])
        command.extend(["-t", "4"])
        filters = []
        if rotate:
            filters.append("transpose=clock")
        if non_square_pixels:
            filters.append("setsar=2/1")
        if filters:
            command.extend(["-vf", ",".join(filters)])
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high"])
        if audio:
            command.extend(["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"])
        else:
            command.extend(["-an"])
        command.extend(["-movflags", "+faststart", str(output)])
        self.run_ffmpeg(*command)
        return output

    def probe(self, path: Path) -> dict:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    def compress(self, source: Path, output: Path, target_mb: str = "2") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "--force", str(source), str(output), target_mb, "0.90", "medium"],
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_compressed_output_is_bounded_and_compatible(self) -> None:
        source = self.make_source("large.mp4")
        output = self.root / "large.whatsapp.mp4"
        result = self.compress(source, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(output.stat().st_size, 2 * 1024 * 1024)
        streams = self.probe(output)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual(video["codec_tag_string"], "avc1")
        self.assertEqual(video["sample_aspect_ratio"], "1:1")
        self.assertEqual(audio["codec_name"], "aac")

    def test_source_without_audio_is_preserved(self) -> None:
        source = self.make_source("silent.mp4", audio=False)
        output = self.root / "silent.whatsapp.mp4"
        result = self.compress(source, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.exists())
        self.assertFalse(any(stream["codec_type"] == "audio" for stream in self.probe(output)["streams"]))

    def test_non_square_pixels_are_normalized_without_stretching_metadata(self) -> None:
        source = self.make_source("sar.mp4", audio=False, non_square_pixels=True)
        output = self.root / "sar.whatsapp.mp4"
        result = self.compress(source, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        video = next(stream for stream in self.probe(output)["streams"] if stream["codec_type"] == "video")
        self.assertEqual(video["sample_aspect_ratio"], "1:1")

    def test_existing_output_is_not_clobbered_without_force(self) -> None:
        source = self.make_source("source.mp4")
        output = self.root / "existing.mp4"
        output.write_bytes(b"keep this file")
        result = subprocess.run(
            ["bash", str(SCRIPT), str(source), str(output), "2", "0.90", "medium"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"keep this file")

    def test_rotation_metadata_keeps_portrait_display_shape(self) -> None:
        source = self.make_source("rotated.mp4", rotate=True)
        output = self.root / "rotated.whatsapp.mp4"
        result = self.compress(source, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        video = next(stream for stream in self.probe(output)["streams"] if stream["codec_type"] == "video")
        self.assertLess(video["width"], video["height"])

    def test_powerpoint_conversion_never_replaces_neighboring_mp4_or_source(self) -> None:
        source = self.root / "clip.mov"
        self.run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
        neighboring_mp4 = self.root / "clip.mp4"
        neighboring_mp4.write_bytes(b"do not replace")
        output, transcoded = media_probe.make_powerpoint_compatible(str(source), "ffmpeg")
        self.assertTrue(transcoded)
        self.assertEqual(Path(output).name, "clip.ppt.mp4")
        self.assertTrue(source.exists())
        self.assertEqual(neighboring_mp4.read_bytes(), b"do not replace")
        self.assertEqual(self.probe(Path(output))["streams"][0]["codec_name"], "h264")
        self.assertTrue(media_probe.media_facts(output, "ffmpeg")["basic_ppt_profile"])


if __name__ == "__main__":
    unittest.main()
