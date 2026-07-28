"""Bounded download orchestration and final media policy."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cache import cache_entry_is_fresh
from constants import (
    DownloadOptions,
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


@dataclass(frozen=True)
class DownloadServices:
    """Runtime tools and provider state supplied by the CLI boundary."""

    yt_dlp: str
    ffmpeg: str
    cookie_browsers: tuple[str, ...] = ()


@dataclass
class AttemptState:
    attempt: RouteResult
    facts: dict[str, object]
    probed: dict[str, object] | None
    attempt_number: int


@dataclass(frozen=True)
class CompatibilityOutcome:
    saved_path: str
    transcoded: bool
    note: str


class BatchBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._committed = 0
        self._reserved = 0
        self._condition = threading.Condition()

    @property
    def used(self) -> int:
        return self._committed + self._reserved

    def reserve(self, amount: int) -> bool:
        if amount <= 0 or amount > self.limit:
            return False
        with self._condition:
            while self._committed + self._reserved + amount > self.limit:
                if self._reserved == 0:
                    return False
                self._condition.wait(timeout=1)
            self._reserved += amount
            return True

    def settle(self, reservation: int, actual: int) -> None:
        actual = max(0, min(actual, reservation))
        with self._condition:
            self._reserved = max(0, self._reserved - reservation)
            self._committed += actual
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


def _cached_result(
    url: str,
    cached_entry: dict[str, object] | None,
    ffmpeg: str,
    started_at: float,
) -> DownloadResult | None:
    if not cached_entry:
        return None
    cached_path = str(cached_entry.get("path", ""))
    if not cache_entry_is_fresh(cached_entry) or not cached_file_is_usable(cached_path, ffmpeg):
        return None
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


def _dry_run_result(url: str, options: DownloadOptions, started_at: float) -> DownloadResult:
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
    elif options.tiktok_shop and options.tiktok_resolver and is_tiktok_url(url):
        message = "would use HTTP resolver providers first"
    else:
        message = "would use yt-dlp social-page flow"
    return DownloadResult(url, True, message, None, DownloadRoute.DRY_RUN, ErrorCode.NONE, metadata)


def _run_tiktok_resolver(
    url: str,
    options: DownloadOptions,
    ffmpeg: str,
    batch_budget: BatchBudget | None,
) -> RouteResult:
    return _run_bounded_route(
        lambda: download_tiktok_via_resolvers(url, options, ffmpeg),
        DownloadRoute.TIKTOK_RESOLVER,
        options.output_dir,
        batch_budget,
    )


def _inspect_attempt(
    attempt: RouteResult, ffmpeg: str
) -> tuple[dict[str, object], dict[str, object] | None]:
    facts: dict[str, object] = {
        "has_video": False,
        "has_audio": False,
        "media_state": "no_media_stream",
        "basic_ppt_profile": False,
    }
    probed = attempt.media_probe
    if attempt.ok and attempt.path:
        if probed is None:
            probed = probe_media(attempt.path, ffmpeg)
        facts = media_facts(attempt.path, ffmpeg, probed=probed)
    return facts, probed


def _initial_attempt(
    url: str,
    options: DownloadOptions,
    services: DownloadServices,
    batch_budget: BatchBudget | None,
) -> AttemptState:
    tiktok_url = is_tiktok_url(url)
    resolver_allowed = options.tiktok_resolver and tiktok_url
    resolver_first = options.tiktok_shop and resolver_allowed
    if resolver_first:
        attempt = _run_tiktok_resolver(url, options, services.ffmpeg, batch_budget)
    elif is_direct_media_url(url):
        attempt = _run_bounded_route(
            lambda: download_direct_media(url, options, services.ffmpeg),
            DownloadRoute.DIRECT,
            options.output_dir,
            batch_budget,
        )
    else:
        attempt = _run_bounded_route(
            lambda: try_download_with_fallbacks(
                url,
                options,
                services.yt_dlp,
                services.ffmpeg,
                services.cookie_browsers,
            ),
            DownloadRoute.SOCIAL,
            options.output_dir,
            batch_budget,
        )

    attempt_number = 1
    if not attempt.ok and resolver_allowed and not resolver_first:
        attempt_number += 1
        attempt = _run_tiktok_resolver(url, options, services.ffmpeg, batch_budget)
    facts, probed = _inspect_attempt(attempt, services.ffmpeg)
    return AttemptState(attempt, facts, probed, attempt_number)


def _retry_with_tiktok_resolver(
    url: str,
    options: DownloadOptions,
    ffmpeg: str,
    state: AttemptState,
    batch_budget: BatchBudget | None,
) -> None:
    state.attempt_number += 1
    state.attempt = _run_tiktok_resolver(url, options, ffmpeg, batch_budget)
    state.facts, state.probed = _inspect_attempt(state.attempt, ffmpeg)


def _validate_media(
    url: str,
    options: DownloadOptions,
    ffmpeg: str,
    state: AttemptState,
    batch_budget: BatchBudget | None,
    started_at: float,
) -> AttemptState | DownloadResult:
    resolver_allowed = options.tiktok_resolver and is_tiktok_url(url)
    if state.attempt.ok and state.attempt.path and not state.facts["has_video"]:
        rejection = (
            "downloaded media contained audio but no video stream"
            if state.facts["has_audio"]
            else "downloaded output contained neither a video nor an audio stream"
        )
        source_error = ErrorCode.AUDIO_ONLY_RESULT if state.facts["has_audio"] else ErrorCode.NO_MEDIA_STREAM
        if resolver_allowed and state.attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
            _retry_with_tiktok_resolver(url, options, ffmpeg, state, batch_budget)
            if state.attempt.ok and state.attempt.path:
                if not state.facts["has_video"]:
                    state.attempt = RouteResult(
                        False,
                        None,
                        state.attempt.route,
                        f"{rejection}; resolver output also had no video stream",
                        state.attempt.browser,
                        source_error,
                        state.attempt.media_probe,
                    )
                    return _failure_result(
                        url,
                        state.attempt,
                        source_error,
                        state.attempt.detail,
                        started_at,
                        media_state=str(state.facts.get("media_state", "no_media_stream")),
                        attempt_number=state.attempt_number,
                        facts=state.facts,
                    )
            else:
                resolver_attempt = state.attempt
                state.attempt = RouteResult(
                    False,
                    None,
                    resolver_attempt.route,
                    f"{rejection}; resolver fallback failed: {resolver_attempt.detail}",
                    error_code=source_error,
                )
                return _failure_result(
                    url,
                    state.attempt,
                    source_error,
                    state.attempt.detail,
                    started_at,
                    media_state=str(state.facts.get("media_state", "no_media_stream")),
                    attempt_number=state.attempt_number,
                    facts=state.facts,
                )
        else:
            state.attempt = RouteResult(
                False,
                None,
                state.attempt.route,
                rejection,
                state.attempt.browser,
                source_error,
            )
            return _failure_result(
                url,
                state.attempt,
                source_error,
                rejection,
                started_at,
                media_state=str(state.facts.get("media_state", "no_media_stream")),
                attempt_number=state.attempt_number,
                facts=state.facts,
            )

    if state.attempt.ok and state.attempt.path and not state.facts["has_audio"]:
        source_can_be_silent = is_direct_media_url(url) or state.attempt.route in {
            DownloadRoute.DIRECT,
            DownloadRoute.HLS_SEGMENTED,
        }
        if not source_can_be_silent:
            if resolver_allowed and state.attempt.route is not DownloadRoute.TIKTOK_RESOLVER:
                _retry_with_tiktok_resolver(url, options, ffmpeg, state, batch_budget)
                if not state.attempt.ok or not state.attempt.path:
                    resolver_detail = state.attempt.detail
                    state.attempt = RouteResult(
                        False,
                        None,
                        state.attempt.route,
                        "downloaded social media had no audio stream; "
                        f"resolver fallback failed: {resolver_detail}",
                        error_code=ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    )
            if not state.attempt.ok or not state.attempt.path or not state.facts["has_audio"]:
                return _failure_result(
                    url,
                    state.attempt,
                    ErrorCode.AUDIO_EXPECTED_BUT_MISSING,
                    state.attempt.detail or "downloaded social media had no audio stream",
                    started_at,
                    media_state="video_only_source",
                    attempt_number=state.attempt_number,
                    facts=state.facts,
                )

    if not state.attempt.ok or not state.attempt.path:
        return _failure_result(
            url,
            state.attempt,
            _error_code_for_attempt(state.attempt),
            state.attempt.detail or "download failed",
            started_at,
            attempt_number=state.attempt_number,
        )
    return state


def _apply_compatibility(
    url: str,
    options: DownloadOptions,
    ffmpeg: str,
    state: AttemptState,
    batch_budget: BatchBudget | None,
    started_at: float,
) -> CompatibilityOutcome | DownloadResult:
    saved_path = state.attempt.path
    assert saved_path is not None
    transcoded = False
    note = ""
    compatibility_reservation = 0
    needs_transcode = options.ppt_compatible and not bool(state.facts.get("basic_ppt_profile", False))
    if needs_transcode and batch_budget is not None:
        compatibility_reservation = MAX_DOWNLOAD_BYTES
        if not batch_budget.reserve(compatibility_reservation):
            return _failure_result(
                url,
                state.attempt,
                ErrorCode.RESOURCE_LIMIT,
                "batch budget is too small for the compatibility transcode",
                started_at,
                media_state=str(state.facts.get("media_state", "no_media_stream")),
                attempt_number=state.attempt_number,
                facts=state.facts,
            )
    if not options.ppt_compatible:
        return CompatibilityOutcome(saved_path, False, "")

    try:
        saved_path, transcoded = make_powerpoint_compatible(
            saved_path,
            ffmpeg,
            force=options.force,
            keep_metadata=options.keep_metadata,
            probed=state.probed,
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
                state.attempt,
                ErrorCode.RESOURCE_LIMIT,
                f"compatibility output exceeded {MAX_DOWNLOAD_BYTES} bytes",
                started_at,
                media_state=str(state.facts.get("media_state", "no_media_stream")),
                attempt_number=state.attempt_number,
                transcoded=transcoded,
                facts=state.facts,
            )

        note = " [basic PowerPoint profile]"
        if not transcoded:
            note = " [basic PowerPoint profile, no re-encode needed]"
        if transcoded:
            final_probe = probe_media(saved_path, ffmpeg)
            state.probed = final_probe
            state.facts = media_facts(saved_path, ffmpeg, probed=final_probe)
        if not state.facts["has_video"]:
            if transcoded:
                Path(saved_path).unlink(missing_ok=True)
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, 0)
                compatibility_reservation = 0
            return _failure_result(
                url,
                state.attempt,
                ErrorCode.NO_MEDIA_STREAM,
                "final output lost its video stream",
                started_at,
                media_state="no_media_stream",
                attempt_number=state.attempt_number,
                transcoded=transcoded,
                facts=state.facts,
            )
        if not state.facts["basic_ppt_profile"]:
            if transcoded:
                Path(saved_path).unlink(missing_ok=True)
            if compatibility_reservation:
                batch_budget.settle(compatibility_reservation, 0)
                compatibility_reservation = 0
            return _failure_result(
                url,
                state.attempt,
                ErrorCode.COMPATIBILITY_VALIDATION_FAILED,
                "final output did not meet the basic H.264/AAC/MP4 profile",
                started_at,
                media_state=str(state.facts.get("media_state", "no_media_stream")),
                attempt_number=state.attempt_number,
                transcoded=transcoded,
                facts=state.facts,
            )
        if compatibility_reservation:
            batch_budget.settle(compatibility_reservation, compatibility_bytes)
            compatibility_reservation = 0
        return CompatibilityOutcome(saved_path, transcoded, note)
    except BaseException:
        if compatibility_reservation:
            batch_budget.settle(compatibility_reservation, 0)
        raise


def _route_note(attempt: RouteResult) -> str:
    if attempt.route is DownloadRoute.DIRECT:
        note = "downloaded_direct"
    elif attempt.route is DownloadRoute.SOCIAL:
        note = "downloaded_social_with_cookies" if attempt.browser else "downloaded_social"
    elif attempt.route is DownloadRoute.HLS_SEGMENTED:
        note = "downloaded_hls_segmented"
    elif attempt.route is DownloadRoute.TIKTOK_RESOLVER:
        note = "downloaded_tiktok_resolver"
    else:
        note = attempt.route.value
    if attempt.detail and attempt.route in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER}:
        note = f"{note}: {attempt.detail}"
    return note


def process_url(
    url: str,
    options: DownloadOptions,
    services: DownloadServices,
    cached_entry: dict[str, object] | None,
    *,
    batch_budget: BatchBudget | None = None,
) -> DownloadResult:
    started_at = time.monotonic()
    cached = _cached_result(url, cached_entry, services.ffmpeg, started_at)
    if cached is not None:
        return cached
    if options.dry_run:
        return _dry_run_result(url, options, started_at)

    state = _initial_attempt(url, options, services, batch_budget)
    validated = _validate_media(url, options, services.ffmpeg, state, batch_budget, started_at)
    if isinstance(validated, DownloadResult):
        return validated

    compatibility = _apply_compatibility(
        url,
        options,
        services.ffmpeg,
        validated,
        batch_budget,
        started_at,
    )
    if isinstance(compatibility, DownloadResult):
        return compatibility

    facts = validated.facts
    saved_path = compatibility.saved_path
    metadata = {
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "from_cache": False,
        "used_cookies": validated.attempt.browser is not None,
        "used_fallback": validated.attempt.route
        in {DownloadRoute.HLS_SEGMENTED, DownloadRoute.TIKTOK_RESOLVER},
        "transcoded": compatibility.transcoded,
        "error_code": ErrorCode.NONE.value,
        "media_state": facts.get("media_state", "audio_and_video"),
        "attempt_number": validated.attempt_number,
        "bytes": Path(saved_path).stat().st_size if Path(saved_path).exists() else 0,
        "has_video": bool(facts.get("has_video", False)),
        "has_audio": bool(facts.get("has_audio", False)),
        "basic_ppt_profile": bool(facts.get("basic_ppt_profile", False)),
    }
    silent_note = " [source has no audio track]" if facts.get("media_state") == "video_only_source" else ""
    return DownloadResult(
        url,
        True,
        f"{_route_note(validated.attempt)}: {saved_path}{compatibility.note}{silent_note}",
        saved_path,
        validated.attempt.route,
        ErrorCode.NONE,
        metadata,
    )
