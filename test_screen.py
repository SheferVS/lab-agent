"""Лента снимков агента без экрана: capture подменён, проверяем PNG, пропуск одинаковых кадров, уборку и
защиту имени. Запуск: python -m pytest tools/lab_agent/test_screen.py"""
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screen  # noqa: E402


def test_png_is_valid_and_pixels_survive():
    w, h = 3, 2
    bgra = bytes([10, 20, 30, 255] * (w * h))
    png = screen.to_png(bgra, w, h)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert raw[:4] == b"\x00" + bytes([30, 20, 10])          # фильтр 0, затем RGB из BGRA


def test_recorder_skips_same_screen_and_ignores_taskbar_clock(tmp_path, monkeypatch):
    frame = bytearray(b"\x01\x02\x03\xff" * 10000)
    monkeypatch.setattr(screen, "capture", lambda mw: (bytes(frame), 100, 100))
    r = screen.Recorder(tmp_path, interval=60)
    assert r.tick() is not None
    frame[-8:] = b"\x09" * 8                                 # «часы» в нижних 6% кадра
    assert r.tick() is None, "смена часов на панели задач — не новый кадр"
    frame[0:4000] = b"\x09" * 4000                           # настоящая смена окна — тысячи байт
    time.sleep(1.1)
    assert r.tick() is not None and len(r.listing()) == 2


def test_prune_and_name_guard(tmp_path, monkeypatch):
    r = screen.Recorder(tmp_path, keep_hours=1)
    old = tmp_path / "20200101-000000.png"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    (tmp_path / "20990101-000000.png").write_bytes(b"y")
    r.prune()
    assert not old.exists() and r.path("20990101-000000.png")
    assert r.path("..\agent.ini") is None and r.path("x.png") is None


# ── помощник в сессии пользователя пишет latest.png, агент отдаёт (агент в сессии 0 экрана не видит) ──

def test_latest_written_atomically_and_staleness_reported(tmp_path):
    screen.write_latest(tmp_path, b"\x89PNG-1")
    body, age = screen.read_latest(tmp_path, 120)
    assert body == b"\x89PNG-1" and age < 5 and not (tmp_path / "latest.tmp").exists()
    old = time.time() - 600
    os.utime(tmp_path / screen.LATEST, (old, old))
    try:
        screen.read_latest(tmp_path, 120)
        raise AssertionError("старый кадр нельзя отдавать как свежий")
    except screen.ScreenError as e:
        assert "назад" in str(e)
    (tmp_path / screen.LATEST).unlink()
    try:
        screen.read_latest(tmp_path, 120)
        raise AssertionError("нет кадра — должна быть причина")
    except screen.ScreenError as e:
        assert "помощник" in str(e)


def test_latest_is_not_part_of_ring(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "capture", lambda mw: (b"\x05\x06\x07\xff" * 100, 10, 10))
    r = screen.Recorder(tmp_path)
    r.tick()
    screen.write_latest(tmp_path, b"x")
    assert [i["name"] for i in r.listing()] != [] and all(i["name"] != "latest.png" for i in r.listing())
