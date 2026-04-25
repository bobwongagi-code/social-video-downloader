import importlib.util
import unittest
import unittest.mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "download_social_video.py"
SPEC = importlib.util.spec_from_file_location("download_social_video", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuildSegmentUrlTests(unittest.TestCase):
    def test_root_relative_segment_resolves_without_double_slash(self) -> None:
        resolved = MODULE.build_segment_url(
            "https://cdn.example.com/hls/master.m3u8?token=abc",
            "/media/seg-1.ts",
        )
        self.assertEqual(resolved, "https://cdn.example.com/media/seg-1.ts?token=abc")

    def test_relative_segment_keeps_existing_query(self) -> None:
        resolved = MODULE.build_segment_url(
            "https://cdn.example.com/hls/master.m3u8?token=abc",
            "chunk.ts?part=1",
        )
        self.assertEqual(resolved, "https://cdn.example.com/hls/chunk.ts?part=1")


class HlsPlaylistParsingTests(unittest.TestCase):
    def test_extract_variant_and_media_entries(self) -> None:
        playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=64000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000
hi/index.m3u8
"""
        media_segments, variants = MODULE.extract_hls_playlist_entries(playlist)
        self.assertEqual(media_segments, [])
        self.assertEqual(variants, [(64000, "low/index.m3u8"), (128000, "hi/index.m3u8")])


class CacheUsabilityTests(unittest.TestCase):
    def test_cached_file_requires_audio(self) -> None:
        original_has_video = MODULE.has_video_stream
        original_has_audio = MODULE.has_audio_stream
        try:
            MODULE.has_video_stream = lambda path, ffmpeg: True
            MODULE.has_audio_stream = lambda path, ffmpeg: False
            with unittest.mock.patch("pathlib.Path.exists", return_value=True), unittest.mock.patch(
                "pathlib.Path.is_file", return_value=True
            ), unittest.mock.patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value.st_size = 10
                self.assertFalse(MODULE.cached_file_is_usable("/tmp/video.mp4", "/usr/bin/ffmpeg"))
        finally:
            MODULE.has_video_stream = original_has_video
            MODULE.has_audio_stream = original_has_audio


if __name__ == "__main__":
    unittest.main()
