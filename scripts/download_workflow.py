"""Bounded download orchestration and final media policy."""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from cache import cache_entry_is_fresh
from constants import (
    DownloadResult,
    DownloadRoute,
    ErrorCode,
    MAX_DOWNLOAD_BYTES,
    MIN_FREE_DISK_BYTES,
    RouteResult,
    redact_text,
)
from download_routes import (
    download_direct_media,
    error_code_from_detail,
    try_download_with_fallbacks,
)
from file_ops import free_bytes
from media_probe import (
    cached_file_is_usable,
    make_powerpoint_compatible,
    media_facts,
    probe_media,
)
from tiktok_resolver import download_tiktok_via_resolvers
from urls import is_direct_media_url, is_tiktok_url


class BatchBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._condition = threading.Condition()

    def reserve(self, amount: int) -> bool:
        if amount <= 0 or amount > self.limit:
            return False
        with self._condition:
            while self.used + amount > self.limit:
                self._condition.wait(timeout=1)
            self.used += amount
            return True

    def settle(self, reservation: int, actual: int) -> None:
        actual = max(0, min(actual, reservation))
        with self._condition:
            self.used = max(0, self.used - reservation) + actual
            self._condition.notify_all()


def _error_code_for_attempt(attempt: RouteResult) -> ErrorCode:
    if attempt.error_code is not ErrorCode.NONE:
        return attempt.error_code
    if attempt.route is DownloadRoute.DIRECT:
        return ErrorCode.DIRECT_DOWNLOAD_FAILED
    if attempt.route is DownloadRoute.HLS_SEGMENTED:
        return ErrorCode.HLS_DOWNLOAD_FAILED
    if attempt.route is DownloadRoute.TIKTOK_RESOLVER:
        return ErrorCode.TIKTOK_RESOLVER_FAILED
    return error_code_from_detail(attempt.detail)


def _failure_result(
    url: str,
    attempt: RouteResult,
    error_code: ErrorCode,
    message: str,
    started_at: float,
    *,
    media_state: str = "no_media_stream",
    attempt_number: int = 1,
    transcoded: bool = False,
    facts: dict[str, object] | None = None,
) -> DownloadResult:
    facts = facts or {}
    metadata = {
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "from_cache": False,
        "used_cookies": attempt.browser is not None,
        "used_fallback": attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER},
        "transcoded": transcoded,
        "error_code": error_code.value,
        "media_state": media_state,
        "attempt_number": attempt_number,
        "has_video": bool(facts.get("has_video", False)),
        "has_audio": bool(facts.get("has_audio", False)),
        "basic_ppt_profile": bool(facts.get("basic_ppt_profile", False)),
    }
    return DownloadResult(url, False, message, None, attempt.route, error_code, metadata)


def _run_bounded_route(
    factory: object,
    route: DownloadRoute,
    output_dir: Path,
    batch_budget: BatchBudget | None,
) -> RouteResult:
    try:
        available = free_bytes(output_dir)
    except OSError as exc:
        return RouteResult(
            False,
            None,
            route,
            f"unable to inspect free disk space: {redact_text(str(exc))}",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )
    if available < MIN_FREE_DISK_BYTES:
        return RouteResult(
            False,
            None,
            route,
            f"less than {MIN_FREE_DISK_BYTES} bytes of free disk space remain",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )

    reservation = MAX_DOWNLOAD_BYTES if batch_budget is not None else 0
    if reservation and not batch_budget.reserve(reservation):
        return RouteResult(
            False,
            None,
            route,
            "batch budget is too small for one download",
            error_code=ErrorCode.RESOURCE_LIMIT,
        )
    try:
        attempt = factory()  # type: ignore[operator]
        if not isinstance(attempt, RouteResult):
            ok, path, detail = attempt
            attempt = RouteResult(bool(ok), path, route, detail)
        actual = 0
        if attempt.ok and attempt.path:
            candidate = Path(attempt.path)
            if candidate.exists():
                actual = candidate.stat().st_size
                if actual > MAX_DOWNLOAD_BYTES:
                    attempt = RouteResult(
                        False,
                        None,
                        attempt.route,
                        f"downloaded file exceeded {MAX_DOWNLOAD_BYTES} bytes",
                        attempt.browser,
                        ErrorCode.RESOURCE_LIMIT,
                    )
        if batch_budget is not None:
            batch_budget.settle(reservation, actual if attempt.ok else 0)
        return attempt
    except BaseException:
        if batch_budget is not None:
            batch_budget.settle(reservation, 0)
        raise


def process_url(
    url: str,
    args: argparse.Namespace,
    yt_dlp: str,
    ffmpeg: str,
    output_dir: Path,
    cookie_browsers: list[str],
    cached_entry: dict[str, object] | None,
    *,
    batch_budget: BatchBudget | None = None,
) -> DownloadResult:
    started_at = time.monotonic()
    if cached_entry:
        cached_path = str(cached_entry.get("path", ""))
        if cache_entry_is_fresh(cached_entry) and cached_file_is_usable(cached_path, ffmpeg):
            metadata = {
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "from_cache": True,
                "used_cookies": False,
                "used_fallback": False,
                "transcoded": False,
                "error_code": ErrorCode.NONE.value,
                "media_state": str(cached_entry.get("media_state", "audio_and_video")),
                "attempt_number": 0,
                "has_video": True,
                "has_audio": bool(cached_entry.get("has_audio", True)),
                "basic_ppt_profile": bool(cached_entry.get("basic_ppt_profile", False)),
                "bytes": Path(cached_path).stat().st_size if Path(cached_path).exists() else 0,
            }
            return DownloadResult(
                url,
                True,
                f"cached file: {cached_path}",
                cached_path,
                DownloadRoute.CACHE,
                ErrorCode.NONE,
                metadata,
            )

    if args.dry_run:
        metadata = {
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "from_cache": False,
            "used_cookies": False,
            "used_fallback": False,
            "transcoded": False,
            "error_code": ErrorCode.NONE.value,
            "has_video": False,
            "has_audio": False,
            "basic_ppt_profile": False,
        }
        if is_direct_media_url(url):
            message = "would download direct media"
        elif args.tiktok_shop and getattr(args, "tiktok_resolver", False) and is_tiktok_url(url):
            message = "would use HTTP resolver providers first"
        else:
            message = "would use yt-dlp social-page flow"
        return DownloadResult(url, True, message, None, DownloadRoute.DRY_RUN, ErrorCode.NONE, metadata)

    tiktok_url = is_tiktok_url(url)
    resolver_allowed = bool(getattr(args, "tiktok_resolver", False)) and tiktok_url
    resolver_first = args.tiktok_shop and resolver_allowed
    if resolver_first:
        attempt = _run_bounded_route(
            lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
            DownloadRoute.TIKTOK_RESOLVER,
            output_dir,
            batch_budget,
        )
    elif is_direct_media_url(url):
        attempt = _run_bounded_route(
            lambda: download_direct_media(url, args, ffmpeg),
            DownloadRoute.DIRECT,
            output_dir,
            batch_budget,
        )
    else:
        attempt = _run_bounded_route(
            lambda: try_download_with_fallbacks(url, args, yt_dlp, ffmpeg, output_dir, cookie_browsers),
            DownloadRoute.SOCIAL,
            output_dir,
            batch_budget,
        )

    attempt_number = 1
    if not attempt.ok and resolver_allowed and not resolver_first:
        attempt_number += 1
        attempt = _run_bounded_route(
            lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
            DownloadRoute.TIKTOK_RESOLVER,
            output_dir,
            batch_budget,
        )

    facts: dict[str, object] = {
        "has_video": False,
        "has_audio": False,
        "media_state": "no_media_stream",
        "basic_ppt_profile": False,
    }
    probed: dict[str, object] | None = attempt.media_probe
    if attempt.ok and attempt.path:
        if probed is None:
            probed = probe_media(attempt.path, ffmpeg)
        facts = media_facts(attempt.path, ffmpeg, probed=probed)

    if attempt.ok and attempt.path and not facts["has_video"]:
        rejection = (
            "downloaded media contained audio but no video stream"
            if facts["has_audio"]
            else "downloaded output contained neither a video nor an audio stream"
        )
        source_error = ErrorCode.AUDIO_ONLY_RESULT if facts["has_audio"] else ErrorCode.NO_MEDIA_STREAM
        if resolver_allowed and attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
            attempt_number += 1
            resolver_attempt = _run_bounded_route(
                lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
                DownloadRoute.TIKTOK_RESOLVER,
                output_dir,
                batch_budget,
            )
            if resolver_attempt.ok and resolver_attempt.path:
                attempt = resolver_attempt
                probed = attempt.media_probe
                if probed is None:
                    probed = probe_media(attempt.path, ffmpeg)
                facts = media_facts(attempt.path, ffmpeg, probed=probed)
                if not facts["has_video"]:
                    attempt = RouteResult(
                        False,
                        None,
                        resolver_attempt.route,
                        f"{rejection}; resolver output also had no video stream",
                        resolver_attempt.browser,
                        source_error,
                        resolver_attempt.media_probe,
                    )
                    return _failure_result(
                        url,
                        attempt,
                        source_error,
                        attempt.detail,
                        started_at,
                        media_state=str(facts.get("media_state", "no_media_stream")),
                        attempt_number=attempt_number,
                        facts=facts,
                    )
            else:
                attempt = RouteResult(
                    False,
                    None,
                    resolver_attempt.route,
                    f"{rejection}; resolver fallback failed: {resolver_attempt.detail}",
                    error_code=source_error,
                )
                return _failure_result(
                    url,
                    attempt,
                    source_error,
                    attempt.detail,
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    facts=facts,
                )
        else:
            attempt = RouteResult(
                False,
                None,
                attempt.route,
                rejection,
                attempt.browser,
                source_error,
            )
            return _failure_result(
                url,
                attempt,
                source_error,
                rejection,
                started_at,
                media_state=str(facts.get("media_state", "no_media_stream")),
                attempt_number=attempt_number,
                facts=facts,
            )

    if attempt.ok and attempt.path and not facts["has_audio"]:
        source_can_be_silent = is_direct_media_url(url) or attempt.route in {
            DownloadRoute.DIRECT,
            DownloadRoute.HLS_SEGMENTED,
        }
        if not source_can_be_silent:
            if resolver_allowed and attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
                attempt_number += 1
                resolver_attempt = _run_bounded_route(
                    lambda: download_tiktok_via_resolvers(url, args, ffmpeg),
                    DownloadRoute.TIKTOK_RESOLVER,
                    output_dir,
                    batch_budget,
                )
                if resolver_attempt.ok and resolver_attempt.path:
                    attempt = resolver_attempt
                    probed = attempt.media_probe
                    if probed is None:
                        probed = probe_media(attempt.path, ffmpeg)
                    facts = media_facts(attempt.path, ffmpeg, probed=probed)
                else:
                    attempt = RouteResult(
                        False,
                        None,
                        resolver_attempt.route,
                        "downloaded social media had no audio stream; "
                        f"resolver fallback failed: {resolver_attempt.detail}",
                        error_code=ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    )
            if not attempt.ok or not attempt.path or not facts["has_audio"]:
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    attempt.detail or "downloaded social media had no audio stream",
                    started_at,
                    media_state="video_only_source",
                    attempt_number=attempt_number,
                    facts=facts,
                )

    if not attempt.ok or not attempt.path:
        return _failure_result(
            url,
            attempt,
            _error_code_for_attempt(attempt),
            attempt.detail or "download failed",
            started_at,
            attempt_number=attempt_number,
        )

    saved_path = attempt.path
    transcoded = False
    compat_note = ""
    compatibility_reservation = 0
    needs_compatibility_transcode = args.ppt_compatible and not bool(
        facts.get("basic_ppt_profile", False)
    )
    if needs_compatibility_transcode and batch_budget is not None:
        compatibility_reservation = MAX_DOWNLOAD_BYTES
        if not batch_budget.reserve(compatibility_reservation):
            return _failure_result(
                url,
                attempt,
                ErrorCode.RESOURCE_LIMIT,
                "batch budget is too small for the compatibility transcode",
                started_at,
                media_state=str(facts.get("media_state", "no_media_stream")),
                attempt_number=attempt_number,
                facts=facts,
            )
    if args.ppt_compatible:
        try:
            saved_path, transcoded = make_powerpoint_compatible(
                saved_path,
                ffmpeg,
                force=getattr(args, "force", False),
                keep_metadata=getattr(args, "keep_metadata", False),
                probed=probed,
            )
            try:
                compatibility_bytes = Path(saved_path).stat().st_size
            except OSError:
                compatibility_bytes = 0
            if compatibility_bytes > MAX_DOWNLOAD_BYTES:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.RESOURCE_LIMIT,
                    f"compatibility output exceeded {MAX_DOWNLOAD_BYTES} bytes",
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )

            compat_note = " [basic PowerPoint profile]"
            if not transcoded:
                compat_note = " [basic PowerPoint profile, no re-encode needed]"
            if transcoded:
                final_probe = probe_media(saved_path, ffmpeg)
                facts = media_facts(saved_path, ffmpeg, probed=final_probe)
            if not facts["has_video"]:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.NO_MEDIA_STREAM,
                    "final output lost its video stream",
                    started_at,
                    media_state="no_media_stream",
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )
            if not facts["basic_ppt_profile"]:
                if transcoded:
                    Path(saved_path).unlink(missing_ok=True)
                if compatibility_reservation:
                    batch_budget.settle(compatibility_reservation, 0)
                    compatibility_reservation = 0
                return _failure_result(
                    url,
                    attempt,
                    ErrorCode.COMPATIBILITY_VALIDATION_FAILED,
                    "final output did not meet the basic H.264/AAC/MP4 profile",
                    started_at,
                    media_state=str(facts.get("media_state", "no_media_stream")),
                    attempt_number=attempt_number,
                    transcoded=transcoded,
                    facts=facts,
                )
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, compatibility_bytes)
                compatibility_reservation = 0
        except BaseException:
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, 0)
            raise

    if attempt.route is DownloadRoute.DIRECT:
        route_note = "downloaded_direct"
    elif attempt.route is DownloadRoute.SOCIAL:
        route_note = "downloaded_social_with_cookies" if attempt.browser else "downloaded_social"
    elif attempt.route is DownloadRoute.HLS_SEGMENTED:
        route_note = "downloaded_hls_segmented"
    elif attempt.route is DownloadRoute.TIKTOK_RESOLVER:
        route_note = "downloaded_tiktok_resolver"
    else:
        route_note = attempt.route.value
    if attempt.detail and attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER}:
        route_note = f"{route_note}: {attempt.detail}"
    metadata = {
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "from_cache": False,
        "used_cookies": attempt.browser is not None,
        "used_fallback": attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER},
        "transcoded": transcoded,
        "error_code": ErrorCode.NONE.value,
        "media_state": facts.get("media_state", "audio_and_video"),
        "attempt_number": attempt_number,
        "bytes": Path(saved_path).stat().st_size if Path(saved_path).exists() else 0,
        "has_video": bool(facts.get("has_video", False)),
        "has_audio": bool(facts.get("has_audio", False)),
        "basic_ppt_profile": bool(facts.get("basic_ppt_profile", False)),
    }
    silent_note = " [source has no audio track]" if facts.get("media_state") == "video_only_source" else ""
    return DownloadResult(
        url,
        True,
        f"{route_note}: {saved_path}{compat_note}{silent_note}",
        saved_path,
        attempt.route,
        ErrorCode.NONE,
        metadata,
    )
