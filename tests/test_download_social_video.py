import importlib.util
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import constants
import hls
import cache as cache_mod
import media_probe
import tiktok_resolver
import download_social_video as main_mod


class BuildSegmentUrlTests(unittest.TestCase):
    def test_root_relative_segment_resolves_without_double_slash(self) -> None:
        resolved = hls.build_segment_url(
            "https://cdn.example.com/hls/master.m3u8?token=abc",
            "/media/seg-1.ts",
        )
        self.assertEqual(resolved, "https://cdn.example.com/media/seg-1.ts?token=abc")

    def test_relative_segment_keeps_existing_query(self) -> None:
        resolved = hls.build_segment_url(
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
        media_segments, variants = hls.extract_hls_playlist_entries(playlist)
        self.assertEqual(media_segments, [])
        self.assertEqual(variants, [(64000, "low/index.m3u8"), (128000, "hi/index.m3u8")])


class CacheUsabilityTests(unittest.TestCase):
    def test_cached_file_requires_audio(self) -> None:
        with unittest.mock.patch.object(
            media_probe, "has_video_stream", return_value=True
        ), unittest.mock.patch.object(
            media_probe, "has_audio_stream", return_value=False
        ), unittest.mock.patch("pathlib.Path.exists", return_value=True), unittest.mock.patch(
            "pathlib.Path.is_file", return_value=True
        ), unittest.mock.patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 10
            self.assertFalse(media_probe.cached_file_is_usable("/tmp/video.mp4", "/usr/bin/ffmpeg"))


class TikTokResolverParsingTests(unittest.TestCase):
    def test_decode_snaptik_response_and_extract_media_url(self) -> None:
        decoded_html = '<a href="https://d.rapidcdn.app/v2?token=x&amp;dl=1">Download</a>'
        payload = "K".join(
            format(ord(character) + 26, "b").replace("0", "J").replace("1", "e")
            for character in decoded_html
        )
        script = f'}}("{payload}",46,"JeKPBURIX",26,2,56))'

        decoded = tiktok_resolver.decode_snaptik_response(script)

        self.assertEqual(decoded, decoded_html.replace("&amp;", "&"))
        self.assertEqual(tiktok_resolver.media_url_candidates(decoded), ["https://d.rapidcdn.app/v2?token=x&dl=1"])


class TikTokResolverRoutingTests(unittest.TestCase):
    def make_args(
        self, output_dir: str, *, tiktok_shop: bool = False, tiktok_resolver: bool = True
    ) -> SimpleNamespace:
        return SimpleNamespace(
            dry_run=False,
            output_dir=output_dir,
            tiktok_shop=tiktok_shop,
            tiktok_resolver=tiktok_resolver,
            ppt_compatible=False,
        )

    def test_known_tiktok_shop_uses_resolver_without_yt_dlp_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            saved_path = str(Path(tmp_dir) / "resolved.mp4")
            with unittest.mock.patch.object(
                main_mod,
                "download_tiktok_via_resolvers",
                return_value=(True, saved_path, "success_tiktok_resolver:snaptik"),
            ) as mock_resolver, unittest.mock.patch.object(
                main_mod, "try_download_with_fallbacks"
            ) as mock_ytdlp, unittest.mock.patch.object(
                main_mod,
                "media_facts",
                return_value={"has_video": True, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    self.make_args(tmp_dir, tiktok_shop=True),
                    "yt-dlp",
                    "ffmpeg",
                    Path(tmp_dir),
                    [],
                    None,
                )

        self.assertTrue(result[1])
        self.assertEqual(result[4], "success_tiktok_resolver:snaptik")
        self.assertTrue(result[5]["used_fallback"])
        mock_resolver.assert_called_once()
        mock_ytdlp.assert_not_called()

    def test_audio_only_tiktok_retries_through_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "audio.m4a"
            audio_path.touch()
            resolved_path = str(Path(tmp_dir) / "resolved.mp4")
            with unittest.mock.patch.object(
                main_mod,
                "try_download_with_fallbacks",
                return_value=(True, str(audio_path), "none"),
            ), unittest.mock.patch.object(
                main_mod,
                "download_tiktok_via_resolvers",
                return_value=(True, resolved_path, "success_tiktok_resolver:snaptik"),
            ) as mock_resolver, unittest.mock.patch.object(
                main_mod,
                "media_facts",
                return_value={"has_video": False, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    self.make_args(tmp_dir),
                    "yt-dlp",
                    "ffmpeg",
                    Path(tmp_dir),
                    [],
                    None,
                )

        self.assertTrue(result[1])
        self.assertEqual(result[4], "success_tiktok_resolver:snaptik")
        self.assertTrue(result[5]["used_fallback"])
        mock_resolver.assert_called_once()

    def test_resolver_opt_out_overrides_tiktok_shop_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            saved_path = str(Path(tmp_dir) / "local.mp4")
            with unittest.mock.patch.object(
                main_mod,
                "try_download_with_fallbacks",
                return_value=(True, saved_path, "none"),
            ) as mock_ytdlp, unittest.mock.patch.object(
                main_mod, "download_tiktok_via_resolvers"
            ) as mock_resolver, unittest.mock.patch.object(
                main_mod,
                "media_facts",
                return_value={"has_video": True, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    self.make_args(tmp_dir, tiktok_shop=True, tiktok_resolver=False),
                    "yt-dlp",
                    "ffmpeg",
                    Path(tmp_dir),
                    [],
                    None,
                )

        self.assertTrue(result[1])
        self.assertEqual(result[4], "success_social")
        mock_ytdlp.assert_called_once()
        mock_resolver.assert_not_called()

    def test_lookalike_domain_never_uses_tiktok_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with unittest.mock.patch.object(
                main_mod,
                "try_download_with_fallbacks",
                return_value=(False, None, "extractor failed"),
            ), unittest.mock.patch.object(main_mod, "download_tiktok_via_resolvers") as mock_resolver:
                result = main_mod.process_url(
                    "https://not-tiktok.com/@shop/video/123456",
                    self.make_args(tmp_dir, tiktok_shop=True),
                    "yt-dlp",
                    "ffmpeg",
                    Path(tmp_dir),
                    [],
                    None,
                )

        self.assertFalse(result[1])
        mock_resolver.assert_not_called()

    def test_resolver_rejects_media_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = self.make_args(tmp_dir)

            def write_candidate(_url: str, destination: Path) -> None:
                destination.touch()

            with unittest.mock.patch.object(
                tiktok_resolver,
                "snaptik_candidates",
                return_value=(["https://cdn.example.com/video.mp4"], "test"),
            ), unittest.mock.patch.object(
                tiktok_resolver, "ssstik_candidates", return_value=([], None)
            ), unittest.mock.patch.object(
                tiktok_resolver, "download_file_via_curl", side_effect=write_candidate
            ), unittest.mock.patch.object(
                tiktok_resolver, "has_video_stream", return_value=True
            ), unittest.mock.patch.object(
                tiktok_resolver, "has_audio_stream", return_value=False
            ):
                ok, detail, extra = tiktok_resolver.download_tiktok_via_resolvers(
                    "https://www.tiktok.com/@shop/video/123456", args, "ffmpeg"
                )

        self.assertFalse(ok)
        self.assertIsNone(detail)
        self.assertIn("without video and audio", extra)


if __name__ == "__main__":
    unittest.main()
