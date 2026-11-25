"""Watchdog behavior without changing any systemd user unit."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

import watchdog_8043 as watchdog


@pytest.fixture
def rig(monkeypatch, tmp_path):
    fixture = SimpleNamespace(activation=10., now=20., healthy=False, probe=[], restarts=[])
    path = tmp_path / "state.json"
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: fixture.now)
    monkeypatch.setattr(watchdog, "active_since", lambda _: fixture.activation)
    monkeypatch.setattr(watchdog, "live", lambda port: fixture.probe.append(port) or fixture.healthy)
    monkeypatch.setattr(watchdog, "systemctl", lambda *args: fixture.restarts.append(args))
    fixture.tick = lambda: watchdog.tick("citrusscholar-8043-candidate-web.service", 18043, path)
    fixture.state = lambda: watchdog.read_state(path)
    return fixture


def test_inactive_cold_and_healthy_never_restart(rig):
    rig.activation = None
    rig.tick()
    rig.activation = 10.
    rig.tick()
    assert not rig.probe
    rig.now = 131.
    rig.healthy = True
    for _ in range(8):
        rig.tick()
    assert rig.probe == [18043] * 8
    assert not rig.restarts


def test_restart_after_consecutive_failures_with_backoff(rig):
    rig.now = 131.
    rig.tick()
    rig.tick()
    assert not rig.restarts
    rig.tick()
    assert rig.restarts == [("try-restart", "--no-block", "citrusscholar-8043-candidate-web.service")]
    assert rig.state()["backoff"] == 120
    rig.activation = 132.
    rig.now = 252.
    for _ in range(3):
        rig.tick()
    assert len(rig.restarts) == 2
    assert rig.state()["backoff"] == 240
    rig.now = 372.
    rig.tick()
    assert len(rig.restarts) == 2
    rig.healthy = True
    rig.tick()
    assert rig.state()["backoff"] == rig.state()["failures"] == 0


def test_replaced_invocation_not_restarted(monkeypatch, rig):
    rig.now = 131.
    rig.tick()
    rig.tick()
    generations = iter((10., 30.))
    monkeypatch.setattr(watchdog, "active_since", lambda _: next(generations))
    rig.tick()
    assert not rig.restarts


def test_probe_is_direct_and_accepts_only_200(monkeypatch):
    calls = []

    class Connection:
        status = 200

        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 18043, 5)

        def request(self, method, path, headers):
            calls.append((method, path))

        def getresponse(self):
            return SimpleNamespace(status=self.status)

        def close(self):
            pass

    monkeypatch.setattr(watchdog, "HTTPConnection", Connection)
    assert watchdog.live(18043)
    Connection.status = 302
    assert not watchdog.live(18043)
    Connection.status = 503
    assert not watchdog.live(18043)
    assert calls == [("GET", "/live")] * 3


def test_cli_rejects_non_web_units_before_using_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    result = subprocess.run([sys.executable, watchdog.__file__, "--unit", "not-a-web.service",
                             "--port", "18043"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "CitrusScholar Web user unit" in result.stderr


def test_cli_tick_uses_a_private_per_unit_state(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [watchdog.__file__, "--unit", "citrusscholar-8043-candidate-web.service",
                                     "--port", "18043"])
    calls = []
    monkeypatch.setattr(watchdog, "tick", lambda *args: calls.append(args))
    watchdog.main()
    (unit, port, state_path), = calls
    assert (unit, port) == ("citrusscholar-8043-candidate-web.service", 18043)
    assert state_path.parent == tmp_path / "citrusscholar-watchdog"
    assert state_path.parent.stat().st_mode & 0o777 == 0o700
    assert state_path.with_suffix(".lock").stat().st_mode & 0o777 == 0o600
