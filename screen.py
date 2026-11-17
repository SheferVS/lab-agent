"""Снимок экрана станции и лента снимков — чтобы в кабинете было видно, что происходит на станции,
не подключаясь к ней удалённо (Виктор 18.11.2026 ~03:55: «давай скриншот даже. Чтобы он понимал, что
происходит. Не подключаясь удаленно даже к машине… подумай, какой интервал скриншотов»; ~04:00 «ок»).

Правила:
- по запросу из кабинета — свежий снимок, ничего не хранится;
- фоном раз в `screen_interval_sec` (по умолчанию 300) — только если экран изменился, хранится на станции
  `screen_keep_hours` (24) и не больше `screen_max_mb` (500 МБ); наружу — только по запросу кабинета;
- на экране данные клиентов: снимки не уходят никуда сами, только через агента (токен + TLS + IP).

Только стандартная библиотека: GDI через ctypes, PNG через zlib. Windows.

Кто снимает. Агент на станции работает задачей планировщика «вне входа» (S4U, сессия 0) — так он доступен
и до входа в Windows, но экрана из сессии 0 не видно (кадр чёрный). Поэтому снимает отдельный помощник
`screen_helper.py` в сессии пользователя (автозапуск при входе): пишет `latest.png` раз в `screen_latest_sec`
и ленту раз в `screen_interval_sec`. Агент только отдаёт файлы из папки — сам экран не трогает.
"""
import os
import ctypes
import hashlib
import logging
import re
import struct
import sys
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

log = logging.getLogger("lab_agent.screen")
NAME_RE = re.compile(r"^\d{8}-\d{6}\.png$")
LATEST = "latest.png"


class ScreenError(RuntimeError):
    pass


def _gdi():
    if sys.platform != "win32":
        raise ScreenError("снимок экрана есть только на Windows")
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    vp, i = ctypes.c_void_p, ctypes.c_int
    # 64-битные дескрипторы: без restype ctypes режет их до int и GDI падает молча
    u.GetDC.restype, u.GetDC.argtypes = vp, [vp]
    u.ReleaseDC.argtypes = [vp, vp]
    g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = vp, [vp]
    g.CreateCompatibleBitmap.restype, g.CreateCompatibleBitmap.argtypes = vp, [vp, i, i]
    g.SelectObject.restype, g.SelectObject.argtypes = vp, [vp, vp]
    g.SetStretchBltMode.argtypes = [vp, i]
    g.SetBrushOrgEx.argtypes = [vp, i, i, vp]
    g.StretchBlt.argtypes = [vp, i, i, i, i, vp, i, i, i, i, ctypes.c_uint32]
    g.GetDIBits.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_uint, vp, vp, ctypes.c_uint]
    g.DeleteObject.argtypes = [vp]
    g.DeleteDC.argtypes = [vp]
    return u, g


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def capture(max_width: int = 1600) -> tuple[bytes, int, int]:
    """Весь виртуальный экран (все мониторы), уменьшенный до max_width. → (BGRA, ширина, высота)."""
    u, g = _gdi()
    try:
        u.SetProcessDPIAware()
    except Exception:
        pass
    x, y, w, h = (u.GetSystemMetrics(n) for n in (76, 77, 78, 79))
    if w <= 0 or h <= 0:
        raise ScreenError("экрана нет (агент вне сессии пользователя?)")
    k = min(1.0, max_width / w)
    tw, th = max(1, int(w * k)), max(1, int(h * k))
    src = u.GetDC(None)
    if not src:
        raise ScreenError("GetDC не дал контекст экрана")
    mem = g.CreateCompatibleDC(src)
    bmp = g.CreateCompatibleBitmap(src, tw, th)
    old = g.SelectObject(mem, bmp)
    try:
        g.SetStretchBltMode(mem, 4)                 # HALFTONE — уменьшение без «лесенки»
        g.SetBrushOrgEx(mem, 0, 0, None)
        if not g.StretchBlt(mem, 0, 0, tw, th, src, x, y, w, h, 0x00CC0020 | 0x40000000):  # SRCCOPY|CAPTUREBLT
            raise ScreenError("StretchBlt не сработал")
        bmi = _BMIH(40, tw, -th, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(tw * th * 4)
        if not g.GetDIBits(mem, bmp, 0, th, buf, ctypes.byref(bmi), 0):
            raise ScreenError("GetDIBits не сработал")
        data = buf.raw
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, src)
    if not any(data[::997]):
        raise ScreenError("кадр чёрный: агент запущен вне сессии пользователя (служба / задача без входа)")
    return data, tw, th


def to_png(bgra: bytes, w: int, h: int) -> bytes:
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[r * stride:(r + 1) * stride] for r in range(h))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def snapshot_png(max_width: int = 1600) -> bytes:
    data, w, h = capture(max_width)
    return to_png(data, w, h)


class Recorder:
    """Фоновая лента: раз в interval — снимок, если экран изменился; старше keep_hours и сверх max_mb — удалить."""

    def __init__(self, folder: Path, interval: int = 300, keep_hours: float = 24, max_mb: int = 500,
                 max_width: int = 1600):
        self.folder, self.interval, self.keep = Path(folder), max(30, interval), keep_hours * 3600
        self.max_bytes, self.max_width = max_mb * 1024 * 1024, max_width
        self.last_hash, self.last_error = None, None
        self.folder.mkdir(parents=True, exist_ok=True)

    def tick(self, frame: tuple[bytes, int, int] | None = None) -> str | None:
        data, w, h = frame or capture(self.max_width)
        # Сравниваем без нижних 6% кадра: часы на панели задач меняются каждую минуту, и иначе «экран
        # изменился» было бы всегда (замер 18.11: два снимка подряд на неподвижном экране — разные).
        digest = hashlib.md5(data[:len(data) * 94 // 100][::61]).hexdigest()
        if digest == self.last_hash:
            return None
        self.last_hash = digest
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"
        (self.folder / name).write_bytes(to_png(data, w, h))
        self.prune()
        return name

    def _ring(self) -> list[Path]:
        return sorted(f for f in self.folder.glob("*.png") if NAME_RE.match(f.name))   # latest.png — не лента

    def prune(self) -> None:
        files = self._ring()
        now, total = time.time(), 0
        for f in reversed(files):                         # свежие сохраняем первыми
            st = f.stat()
            total += st.st_size
            if now - st.st_mtime > self.keep or total > self.max_bytes:
                f.unlink(missing_ok=True)

    def listing(self, since: float = 0) -> list[dict]:
        out = []
        for f in reversed(self._ring()):
            st = f.stat()
            if st.st_mtime >= since:
                out.append({"name": f.name, "at": int(st.st_mtime), "size": st.st_size})
        return out

    def path(self, name: str) -> Path | None:
        if not NAME_RE.match(name or ""):
            return None
        p = self.folder / name
        return p if p.is_file() else None

    def run(self) -> None:
        while True:
            try:
                self.tick()
                self.last_error = None
            except Exception as e:                        # чёрный кадр / экран заблокирован — пишем и ждём
                if str(e) != self.last_error:
                    log.warning("снимок экрана: %s", e)
                self.last_error = str(e)
            time.sleep(self.interval)

    def start(self) -> None:
        threading.Thread(target=self.run, name="screen-recorder", daemon=True).start()


# ── свежий кадр: пишет помощник в сессии пользователя, отдаёт агент ─────────────────────────────

def write_latest(folder: Path, png: bytes) -> None:
    """Атомарно: агент в любой момент читает либо старый целый файл, либо новый целый."""
    folder = Path(folder)
    tmp = folder / "latest.tmp"
    tmp.write_bytes(png)
    os.replace(tmp, folder / LATEST)


def read_latest(folder: Path, max_age: float) -> tuple[bytes, float]:
    """(PNG, возраст в секундах). Нет или старше max_age — ScreenError с причиной, а не старый кадр молча."""
    p = Path(folder) / LATEST
    try:
        st = p.stat()
    except OSError:
        raise ScreenError("свежего кадра нет: помощник снимков (screen_helper.py) не запущен в сессии "
                          "пользователя или в Windows никто не вошёл") from None
    age = time.time() - st.st_mtime
    if age > max_age:
        raise ScreenError(f"последний кадр {int(age)} с назад: помощник снимков остановлен или экран "
                          f"заблокирован (вход пользователя закончен?)")
    return p.read_bytes(), age


def run_helper(folder: Path, latest_sec: int = 30, interval: int = 300, keep_hours: float = 24,
               max_mb: int = 500, max_width: int = 1600) -> None:
    """Цикл помощника: свежий кадр раз в latest_sec, в ленту — раз в interval и только если экран изменился."""
    rec = Recorder(folder, interval=interval, keep_hours=keep_hours, max_mb=max_mb, max_width=max_width)
    next_ring, last_error = 0.0, None
    while True:
        try:
            frame = capture(max_width)
            write_latest(rec.folder, to_png(*frame))
            if time.time() >= next_ring:
                rec.tick(frame)
                next_ring = time.time() + rec.interval
            last_error = None
        except Exception as e:                            # экран заблокирован / нет сессии — пишем один раз
            if str(e) != last_error:
                log.warning("снимок экрана: %s", e)
            last_error = str(e)
        time.sleep(max(5, latest_sec))
