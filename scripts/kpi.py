"""KPI reporting for download metrics."""
from __future__ import annotations

from cache import load_metrics_events
from constants import __version__


def classify_error_category(message: str) -> str:
    if message.startswith("auth_needed:"):
        return "auth_needed"
    if message.startswith("network_unstable:"):
        return "network_unstable"
    if message.startswith("restricted_audio_only:"):
        return "restricted_audio_only"
    if message.startswith("tiktok_resolver_failed:"):
        return "tiktok_resolver_failed"
    if message.startswith("input_invalid:"):
        return "input_invalid"
    return "other_failure"


def render_kpi_report(days: int) -> str:
    events = load_metrics_events(days)
    if not events:
        return f"No KPI events found for the last {days} day(s)."

    simulated_events = [e for e in events if e.get("simulated") is True]
    events = [e for e in events if e.get("simulated") is not True]
    if not events:
        return (
            f"No real download KPI events found for the last {days} day(s). "
            f"Ignored {len(simulated_events)} dry-run event(s)."
        )

    total = len(events)
    success_events = [e for e in events if e.get("success") is True]
    effective_events = [e for e in success_events if e.get("has_video") and e.get("has_audio")]
    first_pass_success = [e for e in effective_events if not e.get("used_cookies") and not e.get("used_fallback")]
    cache_hits = [e for e in events if e.get("from_cache") is True]
    fallback_hits = [e for e in success_events if e.get("used_fallback") is True]
    mis_success = [e for e in success_events if not (e.get("has_video") and e.get("has_audio"))]

    def p50_duration(subset: list[dict[str, object]]) -> int | None:
        values = sorted(int(e["duration_ms"]) for e in subset if isinstance(e.get("duration_ms"), int))
        if not values:
            return None
        return values[len(values) // 2]

    lines = [
        f"KPI report ({days} day window)",
        f"- version: {__version__}",
        f"- total runs: {total}",
        f"- effective delivery rate: {len(effective_events)}/{total} ({len(effective_events) / total:.1%})",
        f"- first-pass success rate: {len(first_pass_success)}/{total} ({len(first_pass_success) / total:.1%})",
        f"- final success rate: {len(success_events)}/{total} ({len(success_events) / total:.1%})",
        f"- false-success rate: {len(mis_success)}/{max(1, len(success_events))} ({len(mis_success) / max(1, len(success_events)):.1%})",
        f"- cache hit rate: {len(cache_hits)}/{total} ({len(cache_hits) / total:.1%})",
        f"- fallback recovery count: {len(fallback_hits)}",
    ]
    if simulated_events:
        lines.append(f"- ignored dry runs: {len(simulated_events)}")

    overall_p50 = p50_duration(events)
    if overall_p50 is not None:
        lines.append(f"- p50 duration: {overall_p50} ms")

    categories: dict[str, int] = {}
    for event in events:
        category = str(event.get("error_category", "none"))
        categories[category] = categories.get(category, 0) + 1
    lines.append("- error categories:")
    for key in sorted(categories):
        lines.append(f"  - {key}: {categories[key]}")

    return "\n".join(lines)
