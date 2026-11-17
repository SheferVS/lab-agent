"""Обновлятор без сети и без планировщика: подменяем GitHub, перезапуск и /health. Проверяем, что
плохая версия откатывается, хорошая встаёт, agent.ini не трогается. python -m pytest tools/lab_agent/test_updater.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater as U  # noqa: E402


def _setup(tmp_path, monkeypatch, *, health_commit=None, bad_code=False):
    monkeypatch.setattr(U, "HERE", tmp_path)
    (tmp_path / "agent.py").write_text("VERSION = '1.2'\n", encoding="utf-8")
    (tmp_path / "agent.ini").write_text("[agent]\ntoken=x\n", encoding="utf-8")
    (tmp_path / "REVISION").write_text("a" * 40, encoding="utf-8")
    new = "b" * 40
    monkeypatch.setattr(U, "remote_sha", lambda repo, br: new)
    body = b"def (:\n" if bad_code else b"VERSION = '1.3'\n"
    monkeypatch.setattr(U, "fetch_release", lambda repo, sha: {"agent.py": body, "screen.py": b"x = 1\n"})
    restarts = []
    monkeypatch.setattr(U, "restart", lambda task: restarts.append(task))
    monkeypatch.setattr(U, "health", lambda cfg, wait=60: {"ok": True, "version": "1.3",
                                                           "commit": (health_commit or new)[:12]})
    return new, restarts


def test_good_release_installed(tmp_path, monkeypatch):
    new, restarts = _setup(tmp_path, monkeypatch)
    msg = U.run_once({"port": 1, "token": "x"}, {"repo": "r", "agent_task": "lab_agent"})
    assert "установлена 1.3" in msg
    assert (tmp_path / "REVISION").read_text() == new and "1.3" in (tmp_path / "agent.py").read_text()
    assert (tmp_path / "agent.ini").read_text().startswith("[agent]") and restarts == ["lab_agent", ""]
    assert U.run_once({}, {"repo": "r"}).startswith("стоит последняя")


def test_agent_not_up_rolls_back_and_marks_bad(tmp_path, monkeypatch):
    new, restarts = _setup(tmp_path, monkeypatch, health_commit="c" * 40)
    msg = U.run_once({"port": 1, "token": "x"}, {"repo": "r", "agent_task": "lab_agent"})
    assert "откатили" in msg
    assert "1.2" in (tmp_path / "agent.py").read_text() and (tmp_path / "REVISION").read_text() == "a" * 40
    assert new in U.load_state()["bad"] and restarts.count("lab_agent") == 2
    assert "уже откатывали" in U.run_once({}, {"repo": "r"})


def test_broken_code_never_installed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, bad_code=True)
    msg = U.run_once({"port": 1, "token": "x"}, {"repo": "r"})
    assert "не взят" in msg and "1.2" in (tmp_path / "agent.py").read_text()
