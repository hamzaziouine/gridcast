"""Heartbeat: optional, never raises, /fail on failure."""

from __future__ import annotations

from gridcast.jobs.runs import heartbeat


def test_heartbeat_is_a_noop_without_url() -> None:
    calls: list[str] = []
    assert heartbeat(None, True, calls.append) == "disabled"
    assert heartbeat("", False, calls.append) == "disabled"
    assert calls == []


def test_heartbeat_success_and_failure_urls() -> None:
    calls: list[str] = []
    assert heartbeat("https://hc.example/ping/x/", True, calls.append) == "sent"
    assert heartbeat("https://hc.example/ping/x", False, calls.append) == "sent"
    assert calls == ["https://hc.example/ping/x", "https://hc.example/ping/x/fail"]


def test_heartbeat_swallows_monitoring_errors() -> None:
    def boom(url: str) -> None:
        raise OSError("network down")

    assert heartbeat("https://hc.example/ping/x", True, boom) == "error"
