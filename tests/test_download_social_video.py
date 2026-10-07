import importlib.util
import contextlib
import io
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
import net
import tiktok_resolver
import urls
import download_routes as routes_mod
import download_workflow as workflow_mod
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

    def test_cross_origin_segment_does_not_receive_playlist_query(self) -> None:
        resolved = hls.build_segment_url(
            "https://playlist.example.com/hls/master.m3u8?token=secret",
            "https://cdn.example.net/media/seg-1.ts",
        )
        self.assertEqual(resolved, "https://cdn.example.net/media/seg-1.ts")


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

    def test_encrypted_playlist_is_rejected_by_simple_fallback(self) -> None:
        with self.assertRaises(hls.UnsupportedHlsPlaylist):
            hls.extract_hls_playlist_entries(
                "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key\"\n#EXTINF:2,\nsegment.ts\n"
            )

    def test_segment_count_is_bounded(self) -> None:
        playlist = "#EXTM3U\n" + "".join(
            f"#EXTINF:2,\nsegment-{index}.ts\n"
            for index in range(constants.HLS_MAX_SEGMENTS + 1)
        )
        with self.assertRaises(hls.UnsupportedHlsPlaylist):
            hls.extract_hls_playlist_entries(playlist)

    def test_live_media_playlist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with unittest.mock.patch.object(
                hls,
                "resolve_hls_media_playlist_url",
                return_value="https://cdn.example.com/live.m3u8",
            ), unittest.mock.patch.object(
                hls,
                "fetch_text_via_curl",
                return_value="#EXTM3U\n#EXTINF:2,\nsegment.ts\n",
            ):
                result = hls.download_hls_via_segments(
                    "https://cdn.example.com/live.m3u8",
                    Path(tmp_dir) / "live.mp4",
                    "ffmpeg",
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.HLS_SEGMENTED)
        self.assertEqual(result.error_code, constants.ErrorCode.HLS_DOWNLOAD_FAILED)
        self.assertIn("ENDLIST", result.detail)


class UrlSafetyTests(unittest.TestCase):
    def test_signed_direct_url_is_preserved_byte_for_byte(self) -> None:
        signed = "https://cdn.example.com/video.mp4?X-Amz-Signature=a%2Fb%3D&ref=required#fragment"
        self.assertEqual(urls.normalize_urls([signed]), [signed])

    def test_social_tracking_parameters_are_removed_without_reserializing_signature(self) -> None:
        social = "https://www.tiktok.com/@name/video/123?xsec_token=a%2Fb%3D&utm_source=share"
        self.assertEqual(
            urls.normalize_urls([social]),
            ["https://www.tiktok.com/@name/video/123?xsec_token=a%2Fb%3D"],
        )

    def test_text_starting_with_http_still_extracts_multiple_urls(self) -> None:
        args = SimpleNamespace(
            inputs=["https://one.example/a text https://two.example/b"],
            text_file=None,
        )
        self.assertEqual(
            urls.collect_urls(args),
            ["https://one.example/a", "https://two.example/b"],
        )

    def test_unsafe_resolved_address_is_rejected(self) -> None:
        with unittest.mock.patch.object(
            net.socket,
            "getaddrinfo",
            return_value=[(net.socket.AF_INET, net.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ):
            with self.assertRaises(net.UnsafeRemoteUrlError):
                net.validate_remote_url("https://example.com/video.m3u8")


class CacheContractTests(unittest.TestCase):
    def test_cache_key_changes_with_output_constraints_and_does_not_store_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with unittest.mock.patch.object(cache_mod, "CACHE_PATH", root / "downloads.json"), unittest.mock.patch.object(
                cache_mod, "CACHE_SALT_PATH", root / ".salt"
            ):
                base = {"url": "https://cdn.example/video.mp4?sig=secret", "max_height": 720}
                changed = {**base, "max_height": 1080}
                first = cache_mod.make_cache_key(base)
                second = cache_mod.make_cache_key(changed)
                self.assertNotEqual(first, second)
                self.assertRegex(first, r"^[0-9a-f]{64}$")
                self.assertFalse((root / "downloads.json").exists())

    def test_cache_key_includes_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with unittest.mock.patch.object(cache_mod, "CACHE_PATH", root / "downloads.json"), unittest.mock.patch.object(
                cache_mod, "CACHE_SALT_PATH", root / ".salt"
            ):
                args = SimpleNamespace(
                    cookies_from_browser=None,
                    auto_cookies=False,
                    max_height=720,
                    ppt_compatible=True,
                    tiktok_shop=False,
                    tiktok_resolver=False,
                )
                first = main_mod.cache_key_for(
                    "https://example.com/video",
                    args,
                    root / "one",
                )
                second = main_mod.cache_key_for(
                    "https://example.com/video",
                    args,
                    root / "two",
                )
                self.assertNotEqual(first, second)

    def test_cache_key_includes_metadata_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with unittest.mock.patch.object(cache_mod, "CACHE_PATH", root / "downloads.json"), unittest.mock.patch.object(
                cache_mod, "CACHE_SALT_PATH", root / ".salt"
            ):
                args = SimpleNamespace(
                    cookies_from_browser=None,
                    auto_cookies=False,
                    max_height=720,
                    ppt_compatible=True,
                    keep_metadata=False,
                    tiktok_shop=False,
                    tiktok_resolver=False,
                )
                first = main_mod.cache_key_for("https://example.com/video", args, root / "out")
                args.keep_metadata = True
                second = main_mod.cache_key_for("https://example.com/video", args, root / "out")
                self.assertNotEqual(first, second)


class ResourceBoundaryTests(unittest.TestCase):
    def test_route_reserves_before_download_and_settles_actual_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "video.mp4"
            budget = workflow_mod.BatchBudget(constants.MAX_DOWNLOAD_BYTES * 2)

            def route() -> constants.RouteResult:
                output.write_bytes(b"x" * 10)
                return constants.RouteResult(True, str(output), constants.DownloadRoute.DIRECT)

            with unittest.mock.patch.object(
                workflow_mod, "free_bytes", return_value=constants.MIN_FREE_DISK_BYTES
            ):
                result = workflow_mod._run_bounded_route(
                    route,
                    constants.DownloadRoute.DIRECT,
                    Path(tmp_dir),
                    budget,
                )
            self.assertTrue(result.ok)
            self.assertEqual(budget.used, 10)

    def test_budget_fails_fast_after_committed_usage_leaves_no_room(self) -> None:
        budget = workflow_mod.BatchBudget(100)
        self.assertTrue(budget.reserve(60))
        budget.settle(60, 60)

        self.assertFalse(budget.reserve(50))
        self.assertEqual(budget.used, 60)

    def test_route_rejects_low_free_space_before_factory_runs(self) -> None:
        factory = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as tmp_dir, unittest.mock.patch.object(
            workflow_mod, "free_bytes", return_value=0
        ):
            result = workflow_mod._run_bounded_route(
                factory,
                constants.DownloadRoute.DIRECT,
                Path(tmp_dir),
                None,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, constants.ErrorCode.RESOURCE_LIMIT)
        self.assertNotIn("resource_limit:", result.detail)
        factory.assert_not_called()


class CliContractTests(unittest.TestCase):
    def test_resolver_defaults_on_but_cookies_and_install_remain_opt_in(self) -> None:
        with unittest.mock.patch.object(
            sys,
            "argv",
            ["download_social_video.py", "https://example.com/video"],
        ):
            args = main_mod.parse_args()
        self.assertFalse(args.install_missing)
        self.assertFalse(args.auto_cookies)
        self.assertTrue(args.tiktok_resolver)

    def test_cli_can_explicitly_disable_tiktok_resolver(self) -> None:
        with unittest.mock.patch.object(
            sys, "argv", ["download_social_video.py", "https://www.tiktok.com/@user/video/123456", "--no-tiktok-resolver"]
        ):
            args = main_mod.parse_args()
        self.assertFalse(args.tiktok_resolver)

    def test_build_command_does_not_embed_metadata_or_force_overwrite_by_default(self) -> None:
        options = constants.DownloadOptions(
            output_dir=Path("/tmp/output"),
            max_height=720,
            ppt_compatible=True,
        )
        with unittest.mock.patch.object(routes_mod, "hash_sensitive_text", return_value="deadbeef"):
            command = main_mod.build_command(
                "https://example.com/video",
                options,
                "yt-dlp",
                "ffmpeg",
            )
        self.assertIn("--no-overwrites", command)
        self.assertNotIn("--embed-metadata", command)
        self.assertNotIn("--force-overwrites", command)


class DryRunContractTests(unittest.TestCase):
    def test_dry_run_does_not_create_output_or_cache_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "new-output"
            args = SimpleNamespace(
                kpi_report=None,
                inputs=["https://www.example.com/video"],
                text_file=None,
                dry_run=True,
                output_dir=str(output_dir),
                tiktok_shop=False,
                tiktok_resolver=False,
                max_height=720,
                concurrency=1,
                auto_cookies=False,
                install_missing=False,
                ppt_compatible=True,
                cookies_from_browser=None,
                force=False,
                keep_metadata=False,
            )
            with unittest.mock.patch.object(main_mod, "parse_args", return_value=args), unittest.mock.patch.object(
                main_mod, "ensure_dependencies", side_effect=AssertionError("dry-run installed dependencies")
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main_mod.main(), 0)
            self.assertFalse(output_dir.exists())


class MediaProbeContractTests(unittest.TestCase):
    def test_display_dimensions_apply_rotation_and_sample_aspect_ratio(self) -> None:
        dimensions = media_probe._display_dimensions(
            {
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "2:1",
                "side_data_list": [{"rotation": 90}],
            }
        )
        self.assertEqual(dimensions, (1080, 3840))


class CacheUsabilityTests(unittest.TestCase):
    def test_cached_video_only_file_is_usable(self) -> None:
        with unittest.mock.patch.object(
            media_probe, "probe_media", return_value={"video": {"codec_name": "h264"}, "audio": {}}
        ), unittest.mock.patch("pathlib.Path.exists", return_value=True), unittest.mock.patch(
            "pathlib.Path.is_file", return_value=True
        ), unittest.mock.patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 10
            self.assertTrue(media_probe.cached_file_is_usable("/tmp/video.mp4", "/usr/bin/ffmpeg"))


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

    def test_current_snaptik_challenge_operations(self) -> None:
        self.assertEqual(
            tiktok_resolver.evaluate_snaptik_challenge({"t": "b", "a": 12, "b": 5, "s": 1}),
            4,
        )
        self.assertEqual(
            tiktok_resolver.evaluate_snaptik_challenge({"t": "r", "n": [1, 2, 3]}),
            13,
        )
        self.assertEqual(
            tiktok_resolver.evaluate_snaptik_challenge({"t": "c", "w": "abc", "i": 1, "m": 2}),
            196,
        )
        self.assertEqual(
            tiktok_resolver.evaluate_snaptik_challenge({"t": "m", "a": 7, "b": 8, "c": 3}),
            45,
        )
        self.assertEqual(
            tiktok_resolver.evaluate_snaptik_challenge({"t": "n", "a": 8, "b": 6, "c": 2}),
            68,
        )

    def test_current_snaptik_api_response_is_parsed(self) -> None:
        with unittest.mock.patch.object(
            tiktok_resolver,
            "curl_text_request",
            side_effect=[
                "homepage",
                '{"id":"token-1","p":"encrypted"}',
                '{"data":{"title":"Current title","downloadUrl":"https://cdn.example/video?token=x","hdDownloadUrl":"https://cdn.example/hd?token=y"}}',
            ],
        ) as request, unittest.mock.patch.object(
            tiktok_resolver, "solve_snaptik_challenge", return_value="token-1:42:e:h"
        ):
            candidates, title = tiktok_resolver.snaptik_candidates(
                "https://www.tiktok.com/@shop/video/123456"
            )

        self.assertEqual(candidates, ["https://cdn.example/video?token=x", "https://cdn.example/hd?token=y"])
        self.assertEqual(title, "Current title")
        self.assertEqual(request.call_args_list[1].args[0], constants.SNAPTIK_TOKEN_URL)
        self.assertEqual(request.call_args_list[1].kwargs["form_fields"], [])
        self.assertIn("Content-Type: application/json", request.call_args_list[1].kwargs["headers"])
        extract_url = request.call_args_list[2].args[0]
        self.assertIn("/api/extract?url=https%3A%2F%2Fwww.tiktok.com%2F%40shop%2Fvideo%2F123456", extract_url)
        self.assertIn("X-Verify: token-1:42:e:h", request.call_args_list[2].kwargs["headers"])

    def test_current_ssstik_form_configuration_and_media_link_are_parsed(self) -> None:
        with unittest.mock.patch.object(
            tiktok_resolver,
            "curl_text_request",
            side_effect=[
                "<script>var s_furl = 'abc'; var s_tt = 'Vm5BTlIy';</script>",
                '<p>Current title</p><a href="https://cdn.example/video?token=x">Download</a>',
            ],
        ) as request:
            candidates, title = tiktok_resolver.ssstik_candidates(
                "https://www.tiktok.com/@shop/video/123456"
            )

        self.assertEqual(candidates, ["https://cdn.example/video?token=x"])
        self.assertEqual(title, "Current title")
        self.assertEqual(request.call_args_list[1].args[0], "https://ssstik.io/abc?url=dl")
        self.assertIn(("tt", "Vm5BTlIy"), request.call_args_list[1].kwargs["form_fields"])
        self.assertIn("HX-Trigger: _gcaptcha_pt", request.call_args_list[1].kwargs["headers"])


class TikTokResolverRoutingTests(unittest.TestCase):
    def make_options(
        self, output_dir: str, *, tiktok_shop: bool = False, tiktok_resolver: bool = True
    ) -> constants.DownloadOptions:
        return constants.DownloadOptions(
            dry_run=False,
            output_dir=Path(output_dir),
            tiktok_shop=tiktok_shop,
            tiktok_resolver=tiktok_resolver,
            ppt_compatible=False,
        )

    def test_default_tiktok_uses_resolver_without_yt_dlp_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            saved_path = str(Path(tmp_dir) / "resolved.mp4")
            with unittest.mock.patch.object(
                workflow_mod,
                "download_tiktok_via_resolvers",
                return_value=constants.RouteResult(
                    True,
                    saved_path,
                    constants.DownloadRoute.TIKTOK_RESOLVER,
                    "snaptik",
                ),
            ) as mock_resolver, unittest.mock.patch.object(
                workflow_mod, "try_download_with_fallbacks"
            ) as mock_ytdlp, unittest.mock.patch.object(
                workflow_mod,
                "media_facts",
                return_value={"has_video": True, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    constants.DownloadOptions(output_dir=Path(tmp_dir), ppt_compatible=False),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"),
                    None,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.TIKTOK_RESOLVER)
        self.assertIn("downloaded_tiktok_resolver: snaptik:", result.message)
        self.assertTrue(result.metadata["used_fallback"])
        mock_resolver.assert_called_once()
        mock_ytdlp.assert_not_called()

    def test_default_dry_run_reports_resolver_route(self) -> None:
        result = main_mod.process_url(
            "https://www.tiktok.com/@user/video/123456",
            constants.DownloadOptions(output_dir=Path("/tmp/output"), dry_run=True),
            workflow_mod.DownloadServices("yt-dlp", "ffmpeg"), None,
        )
        self.assertIn("HTTP resolver providers first", result.message)

    def test_tiktok_direct_media_still_bypasses_page_resolvers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with unittest.mock.patch.object(
                workflow_mod, "download_direct_media",
                return_value=constants.RouteResult(True, str(Path(tmp_dir) / "direct.mp4"), constants.DownloadRoute.DIRECT),
            ) as mock_direct, unittest.mock.patch.object(
                workflow_mod, "download_tiktok_via_resolvers"
            ) as mock_resolver, unittest.mock.patch.object(
                workflow_mod, "media_facts", return_value={"has_video": True, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/video.mp4", self.make_options(tmp_dir),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"), None,
                )
        self.assertTrue(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.DIRECT)
        mock_direct.assert_called_once()
        mock_resolver.assert_not_called()

    def test_audio_only_resolver_result_fails_without_yt_dlp_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "audio.m4a"
            audio_path.touch()
            with unittest.mock.patch.object(
                workflow_mod,
                "try_download_with_fallbacks",
                return_value=(True, str(audio_path), "none"),
            ) as mock_ytdlp, unittest.mock.patch.object(
                workflow_mod,
                "download_tiktok_via_resolvers",
                return_value=constants.RouteResult(
                    True,
                    str(audio_path),
                    constants.DownloadRoute.TIKTOK_RESOLVER,
                    "snaptik",
                ),
            ) as mock_resolver, unittest.mock.patch.object(
                workflow_mod,
                "media_facts",
                return_value={"has_video": False, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    self.make_options(tmp_dir),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"),
                    None,
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.TIKTOK_RESOLVER)
        self.assertEqual(result.error_code, constants.ErrorCode.AUDIO_ONLY_RESULT)
        mock_resolver.assert_called_once()
        mock_ytdlp.assert_not_called()

    def test_failed_resolver_does_not_fall_back_to_yt_dlp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with unittest.mock.patch.object(
                workflow_mod, "download_tiktok_via_resolvers",
                return_value=constants.RouteResult(False, None, constants.DownloadRoute.TIKTOK_RESOLVER, "providers failed"),
            ) as mock_resolver, unittest.mock.patch.object(
                workflow_mod, "try_download_with_fallbacks"
            ) as mock_ytdlp:
                result = main_mod.process_url(
                    "https://www.tiktok.com/@user/video/123456", self.make_options(tmp_dir),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"), None,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.TIKTOK_RESOLVER)
        mock_resolver.assert_called_once()
        mock_ytdlp.assert_not_called()

    def test_resolver_opt_out_overrides_tiktok_shop_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            saved_path = str(Path(tmp_dir) / "local.mp4")
            with unittest.mock.patch.object(
                workflow_mod,
                "try_download_with_fallbacks",
                return_value=(True, saved_path, "none"),
            ) as mock_ytdlp, unittest.mock.patch.object(
                workflow_mod, "download_tiktok_via_resolvers"
            ) as mock_resolver, unittest.mock.patch.object(
                workflow_mod,
                "media_facts",
                return_value={"has_video": True, "has_audio": True},
            ):
                result = main_mod.process_url(
                    "https://www.tiktok.com/@shop/video/123456",
                    self.make_options(tmp_dir, tiktok_shop=True, tiktok_resolver=False),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"),
                    None,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.route, constants.DownloadRoute.SOCIAL)
        mock_ytdlp.assert_called_once()
        mock_resolver.assert_not_called()

    def test_lookalike_domain_never_uses_tiktok_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with unittest.mock.patch.object(
                workflow_mod,
                "try_download_with_fallbacks",
                return_value=(False, None, "extractor failed"),
            ), unittest.mock.patch.object(workflow_mod, "download_tiktok_via_resolvers") as mock_resolver:
                result = main_mod.process_url(
                    "https://not-tiktok.com/@shop/video/123456",
                    self.make_options(tmp_dir, tiktok_shop=True),
                    workflow_mod.DownloadServices("yt-dlp", "ffmpeg"),
                    None,
                )

        self.assertFalse(result.ok)
        mock_resolver.assert_not_called()

    def test_resolver_rejects_media_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            options = self.make_options(tmp_dir)

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
                tiktok_resolver,
                "media_facts",
                return_value={"has_video": True, "has_audio": False},
            ):
                result = tiktok_resolver.download_tiktok_via_resolvers(
                    "https://www.tiktok.com/@shop/video/123456", options, "ffmpeg"
                )

        self.assertFalse(result.ok)
        self.assertIsNone(result.path)
        self.assertIn("without video and audio", result.detail)


if __name__ == "__main__":
    unittest.main()
