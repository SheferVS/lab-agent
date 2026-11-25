"""Пул данных станции: какие заказы лежат на её дисках, и выгрузка готовой папки на сетевую шару.

Задача 2480 «Модуль выдачи восстановленных данных» (ТЗ TASK_data_delivery_2026-07-31.md, §0 — пересмотр 19.11.2026).
Работает сам, без людей и без сессий: индекс строится в фоне, копию запускает бэк. Только стандартная библиотека.

ИНДЕКС (`Index`). Все готовые локальные тома, кроме системного и перечисленных в `index_skip`, на глубину 1–2.
Папка попадает в индекс, если у неё есть хоть один признак заказа:
  - номер заказа в имени: `46154_data`, `К46154`, `46154-1` (5–6 цифр подряд);
  - паспорт задачи PC-3000 — файл `*.info` (обычно task.info): модель, прошивка, серийник накопителя;
  - образ: посекторные куски PC-3000 (`img<LBA>.bin` + `Head.map`), хранилище DE (`*.deff`), файл-образ
    (`.img/.dd/.raw/.vhd/.vhdx/.e01`). Образ ищется в самой папке и в её подпапках первого уровня.
Подпапки папки с признаком (RIK, Users внутри `46154_data`) — содержимое заказа: в индекс попадают, только если
у них свой номер в имени или свой паспорт. Размер папок индекс не считает (это обход тысяч файлов на каждом
проходе) — объём считает выгрузка.

ВЫГРУЗКА (`Copier`). Папку из индекса — в корень `delivery_root` (UNC-путь шары, например \\\\host\\share) под
тем же именем. Копия, не перенос: источник не трогается, ничего не удаляется ни там, ни там. Файл, который уже
лежит в копии с тем же размером и временем, повторно не копируется — обрыв докачивается. В конце — сверка числа
файлов и байт источника и копии. Агент работает службой Windows: сетевой диск, подключённый в сессии
пользователя (Z:), ему не виден, поэтому путь — UNC, а учётка шары (если нужна) — `delivery_user` /
`delivery_password` в agent.ini.
"""
from __future__ import annotations

import codecs
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("lab_agent.datapool")

# Номер заказа — только В НАЧАЛЕ имени: «46154», «46154_data», «data_46154», «К46154», «45801-img». Число в
# середине («Acronis … build 39287», «Camtasia … 32207» — H:\ сервера 19.11) номером заказа не считается: по
# номеру бэк привязывает папку к заказу сам, ложный номер отдал бы чужие данные.
ORDER_RE = re.compile(r"^(?:[КK]|data[ _-]*)?(\d{5,6})(?![0-9])", re.I)


def order_nums(name: str) -> list[str]:
    m = ORDER_RE.match(name.strip())
    return [m.group(1)] if m else []
DATA_RE = re.compile(r"(?i)(?<![a-z])data(?![a-z])")
CHUNK_RE = re.compile(r"(?i)^img\d+\.bin$")
FILE_IMAGE_EXT = {".img", ".dd", ".raw", ".vhd", ".vhdx", ".e01", ".ima"}
FILE_IMAGE_MIN = 64 * 1024 * 1024
INFO_MAX = 16 * 1024
DE_MIN_BYTES = 1024 * 1024  # хранилище DE меньше — пустое (заголовок), образа в нём нет
BACKUP_DIRS = {"!backup"}   # PC-3000 кладёт туда копии task.info / task_*.prm — это не отдельная задача
SKIP_ROOT = {"system volume information", "windows", "program files", "program files (x86)", "programdata",
             "recovery", "config.msi", "msocache", "perflogs", "boot", "users"}
MAX_SUBDIRS = 200          # подпапок, которые смотрим у одной папки (глубина 2 и поиск образа)
MAX_FOLDERS = 20_000       # предел индекса — защита от тома с миллионом папок в корне
SCAN_BUDGET_SEC = 600


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── паспорт задачи (task.info) ─────────────────────────────────────────────────────────────────────────────

def decode_text(raw: bytes) -> str:
    """task.info пишет PC-3000 в cp1251; на всякий случай — BOM UTF-8/UTF-16 и чистый UTF-8."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


def _device(tokens: list[str]) -> dict:
    """«<модель…> <прошивка> <серийник>» — модель может быть из нескольких слов, прошивка и серийник — по одному."""
    if len(tokens) >= 3:
        return {"model": " ".join(tokens[:-2]), "fw": tokens[-2], "serial": tokens[-1]}
    if len(tokens) == 2:
        return {"model": tokens[0], "serial": tokens[1]}
    return {"model": tokens[0]} if tokens else {}


def parse_info(raw: bytes) -> dict:
    """Паспорт задачи PC-3000. Встречаются три вида (LAB-VLAD, 19.11):
         Source -PC3000 SATA1                         ← порт, накопитель — следующей строкой:
                     <модель> <прошивка> <серийник>
         Source -<модель> <прошивка> S/N:<серийник>    ← всё в одной строке
         Source -PC3000 USB                           ← без модели
       плюс «Destin -File Image» / «Destin -DE-Storage» (может не быть) и первая строка — что за задача.
       Что не разобрали — остаётся в `text`: бэк ищет серийник и по сырому тексту.
       Форма «S/N:» живьём (C:\\Program Files\\ACE Lab\\PC-3000\\Bin сервера, 19.11): «HKVSN PHDD-T30/1TB  S/N:…»,
       «Generic STORAGE DEVICE  S/N:…» — модель без прошивки."""
    text = decode_text(raw[:INFO_MAX])
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: dict = {"text": text.strip()[:2000]}
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    if nonempty and not re.match(r"(?i)(source|destin)\s*-", nonempty[0]):
        out["title"] = nonempty[0]
    for i, ln in enumerate(lines):
        m = re.match(r"(?i)\s*source\s*-\s*(.*)$", ln)
        if m:
            src = m.group(1).strip()
            out["source"] = src
            if re.search(r"(?i)s/n\s*:", src):
                # «HKVSN PHDD-T30/1TB  S/N:DD56419883C31» (PC-3000\Bin\45795): прошивки в этой форме нет —
                # модель вся строка до S/N (обычно имя USB-моста, а не накопителя)
                before, after = re.split(r"(?i)s/n\s*:", src, maxsplit=1)
                if before.strip():
                    out["model"] = " ".join(before.split())
                out["serial"] = (after.split() or [""])[0]
            elif re.match(r"(?i)pc-?3000\b", src):
                out["port"] = src
                for nxt in lines[i + 1:]:
                    s = nxt.strip()
                    if not s:
                        continue
                    if not re.match(r"(?i)(destin|source)\s*-", s):
                        out.update(_device(s.split()))
                    break
            else:
                out.update(_device(src.split()))
            continue
        m = re.match(r"(?i)\s*destin\s*-\s*(.*)$", ln)
        if m:
            out["destin"] = m.group(1).strip()
    return out


def read_shared(path: str, limit: int = INFO_MAX) -> bytes:
    """Прочитать файл, не мешая тому, кто его держит: на дисках станции идёт снятие в PC-3000 DE (замечание
    станции 19.11). Windows — CreateFileW с FILE_SHARE_READ|WRITE|DELETE (обычный open() не разделяет удаление);
    иначе — обычное чтение."""
    if os.name != "nt":
        with open(path, "rb") as f:
            return f.read(limit)
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                             wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, SHARE_ALL, OPEN_EXISTING = 0x80000000, 0x7, 3
    h = k32.CreateFileW(long_path(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"не открыть {path}")
    try:
        buf = ctypes.create_string_buffer(limit)
        got = wintypes.DWORD(0)
        if not k32.ReadFile(h, buf, limit, ctypes.byref(got), None):
            raise OSError(ctypes.get_last_error(), f"не прочитать {path}")
        return buf.raw[:got.value]
    finally:
        k32.CloseHandle(h)


# ── одна папка ─────────────────────────────────────────────────────────────────────────────────────────────
# Индекс только листает каталоги (FindFirstFile — без открытия файлов; размеры и время — из листинга) и читает
# task.info через read_shared. Образы и данные не открываются никогда.

def _mtime(e: os.DirEntry) -> float:
    try:
        return e.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0


def _scandir(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _images_in(entries: list[os.DirEntry]) -> list[dict]:
    """Образы среди файлов одной папки. На Windows DirEntry.stat() берётся из листинга — без лишних обращений."""
    chunks = {"files": 0, "bytes": 0}
    deff = {"files": 0, "bytes": 0}
    files: list[dict] = []
    names = set()
    for e in entries:
        if not e.is_file(follow_symlinks=False):
            continue
        name = e.name
        names.add(name.lower())
        try:
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0
        ext = os.path.splitext(name)[1].lower()
        if CHUNK_RE.match(name):
            chunks["files"] += 1
            chunks["bytes"] += size
        elif ext == ".deff":
            deff["files"] += 1
            deff["bytes"] += size
        elif ext in FILE_IMAGE_EXT and size >= FILE_IMAGE_MIN:
            files.append({"kind": "file-image", "name": name, "files": 1, "bytes": size})
    out = []
    if chunks["files"] and ("head.map" in names or "task.fdb" in names or chunks["files"] > 1):
        out.append({"kind": "pc3000-chunks", **chunks})
    if deff["files"]:
        out.append({"kind": "deff", **deff})
    return out + files[:10]


def _store_files(path: str, prefix: str, ext: str) -> dict:
    files = [e for e in _scandir(path) if e.is_file(follow_symlinks=False)
             and e.name.lower().startswith(prefix) and e.name.lower().endswith(ext)]
    return {"files": len(files), "bytes": sum(e.stat(follow_symlinks=False).st_size for e in files)}


TASK_TYPES = {
    "de-storage": "образ в хранилище DE (DSFStor.dsf / DriveVersions.deff) — читается без PC-3000 (формат 3814)",
    "file-image": "посекторный образ файлами (img<LBA>.bin) или одним файлом — читается без PC-3000",
    "patient": "работа напрямую с пациентом: образа в папке нет, данные — только на самом накопителе",
}


def task_type_of(info: dict | None, images: list[dict]) -> tuple[str | None, str | None]:
    """Тип задачи PC-3000 — три варианта (Виктор 19.11): напрямую с пациентом, образ файлами, хранилище DE.
    Признаки два: что задача сказала о себе (`Destin` в task.info) и что реально лежит в папке. Расходятся —
    верим папке и говорим об этом: «Destin -File Image», а кусков нет — образ унесли или не дописали."""
    destin = ((info or {}).get("destin") or "").lower()
    # пустое хранилище DE — только заголовок (у задачи-кусков DriveVersions.deff по 198 байт, H:\…обменка_ноут)
    de_bytes = sum(i.get("bytes") or 0 for i in images if i["kind"] in ("de-storage", "de-versions", "deff"))
    img_bytes = sum(i.get("bytes") or 0 for i in images if i["kind"] in ("pc3000-chunks", "file-image"))
    by_files = ("de-storage" if de_bytes >= DE_MIN_BYTES and de_bytes >= img_bytes
                else "file-image" if img_bytes else None)
    by_info = "de-storage" if "storage" in destin else "file-image" if "image" in destin else None
    if by_files:
        note = None if by_info in (None, by_files) else f"task.info: «{info.get('destin')}», а в папке — {by_files}"
        return by_files, note
    if by_info:
        return by_info, f"task.info: «{info.get('destin')}», но образа в папке нет — унесли или не записан"
    if info is not None:
        return "patient", None
    return None, None


def describe(path: str, *, depth: int, volume: str) -> tuple[dict | None, list[os.DirEntry]]:
    """Запись индекса о папке (или None, если признаков заказа нет) + её подпапки."""
    entries = _scandir(path)
    subdirs = [e for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith("$")
               and e.name.lower() not in BACKUP_DIRS]
    name = os.path.basename(path.rstrip("\\/"))
    nums = order_nums(name)
    # время папки — по самому свежему из листинга (без открытия): «в ней сейчас пишут» видно в дашборде
    newest = max((_mtime(e) for e in entries if e.is_file(follow_symlinks=False)), default=None)
    info = None
    infos = sorted((e for e in entries if e.is_file(follow_symlinks=False) and e.name.lower().endswith(".info")),
                   key=lambda e: (e.name.lower() != "task.info", e.name.lower()))
    for cand in infos[:5]:
        # *.info бывает чей угодно (игры, установщики): паспорт PC-3000 — только со строкой Source/Destin
        try:
            parsed = parse_info(read_shared(cand.path))
        except OSError as e:
            if cand.name.lower() == "task.info":
                info = {"file": cand.name, "error": str(e)[:200]}
                break
            continue
        if "source" in parsed or "destin" in parsed:
            info = {"file": cand.name, **parsed}
            break
    images = [dict(i, where="") for i in _images_in(entries)]
    de_storage = any(sd.name.lower() == "dsfstor.dsf" for sd in subdirs)     # признак задачи DE (формат 3814)
    if not (nums or info or images or de_storage):
        # образ в подпапке сам по себе папку не делает заказом: «PC3000\Tasks» с задачами внутри — не заказ,
        # его подпапки индекс посмотрит на глубине 2
        return None, subdirs
    for sd in subdirs[:MAX_SUBDIRS]:
        if sd.name.lower() == "dsfstor.dsf":
            images.append(dict(_store_files(sd.path, "dsfdata_", ".dsf"), kind="de-storage", where=sd.name))
            continue
        sub = _scandir(sd.path)
        images += [dict(i, where=sd.name) for i in _images_in(sub)]
        for dv in sub:                                   # <N>\DriveVersions.deff\DataFile_NNNN.deff
            if dv.is_dir(follow_symlinks=False) and dv.name.lower() == "driveversions.deff":
                images.append(dict(_store_files(dv.path, "datafile_", ".deff"), kind="de-versions",
                                   where=f"{sd.name}\\{dv.name}"))
    task_type, type_note = task_type_of(info, images)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = None
    rec = {"path": path, "name": name, "volume": volume, "depth": depth, "mtime": _iso(mtime),
           "newest_file_at": _iso(newest),
           "order_nums": nums, "is_data": bool(nums and DATA_RE.search(name)), "info": info,
           "task_type": task_type, "task_type_note": type_note,
           "images": images, "subdirs": [d.name for d in subdirs[:20]], "subdirs_total": len(subdirs)}
    return rec, subdirs


def scan(volumes: list[dict], *, skip: set[str] = frozenset(), budget_sec: float = SCAN_BUDGET_SEC,
         extra_roots: list = ()) -> dict:
    """Индекс по готовым локальным томам. `volumes` — как отдаёт агент (`list_volumes`); `extra_roots` — папки
    задач на пропускаемых томах (PC-3000\\Bin на C:)."""
    started = time.monotonic()
    folders: list[dict] = []
    errors: list[str] = []
    seen_vol = []
    for v in volumes:
        letter = (v.get("letter") or "").upper()
        if not letter or v.get("ready") is False or v.get("type") != "fixed" or letter in skip:
            continue
        root = v.get("root") or f"{letter}:\\"          # root — только для тестов: том из временной папки
        seen_vol.append(letter)
        for top in _scandir(root):
            if time.monotonic() - started > budget_sec or len(folders) >= MAX_FOLDERS:
                errors.append(f"скан прерван на {top.path}: предел времени или числа папок")
                break
            if not top.is_dir(follow_symlinks=False) or top.name.startswith("$") \
                    or top.name.lower() in SKIP_ROOT:
                continue
            rec, subdirs = describe(top.path, depth=1, volume=letter)
            if rec:
                folders.append(rec)
            for sd in subdirs[:MAX_SUBDIRS]:
                if rec and not order_nums(sd.name):
                    # содержимое заказа: внутрь не лезем, кроме подпапки со своим паспортом
                    if not any(e.name.lower().endswith(".info") for e in _scandir(sd.path)
                               if e.is_file(follow_symlinks=False)):
                        continue
                child, _ = describe(sd.path, depth=2, volume=letter)
                if child:
                    folders.append(child)
        else:
            continue
        break
    # Каталоги задач самой PC-3000 на системном томе (Виктор 19.11: «На C в папке программ files есть каталог bin
    # там тоже много задач… 99% как раз те что без образа»). Только глубина 1: внутри задачи — её служебное.
    for item in extra_roots:
        root, product = (item, None) if isinstance(item, str) else (item[0], item[1])
        if not os.path.isdir(root):
            continue
        seen_vol.append(root)
        for top in _scandir(root):
            if not top.is_dir(follow_symlinks=False) or top.name.startswith(("!", "$")) \
                    or top.name.lower() in EXTRA_SKIP:
                continue
            rec, _ = describe(top.path, depth=1, volume=os.path.splitdrive(root)[0].rstrip(":").upper() or "?")
            if rec:
                rec["root"] = root
                rec["product"] = product          # какой PC-3000 ведёт задачу (Express / Portable)
                folders.append(rec)
    return {"scanned_at": _iso(time.time()), "duration_sec": round(time.monotonic() - started, 1),
            "volumes": seen_vol, "folders": folders, "errors": errors}


EXTRA_SKIP = {"repository", "temp", "logs", "plugins"}


def _installed_pc3000() -> list[tuple[str, str]]:
    """Установки PC-3000 из реестра (Uninstall, обе ветки — 64 и 32 бит): [(папка установки, название)].
    На станции их бывает несколько: Express и Portable — каждая со своей папкой и своим Bin (Виктор 19.11)."""
    if os.name != "nt":
        return []
    import winreg
    out = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0,
                                  winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DisplayName")[0] or "")
                        loc = str(winreg.QueryValueEx(k, "InstallLocation")[0] or "")
                except OSError:
                    continue
                if loc and re.search(r"(?i)pc-?3000|ace lab|data extractor", name):
                    out.append((loc, name))
    return out


def pc3000_task_roots() -> list[tuple[str, str]]:
    """Папки задач PC-3000 (Bin) на этой машине: [(путь, какой PC-3000)].

    Виктор 19.11: «есть папочка для Portable отдельно, есть папочка для Express отдельно. И у каждого есть папка
    BIN… Еще есть папка Program Files x86, а есть для 64-разрядная». Ищем двумя путями: установки из реестра
    (InstallLocation\\Bin) и любые <Program Files | Program Files (x86)>\\ACE Lab\\<что угодно>\\Bin. На сервере
    19.11: «PC-3000 Portable Setup (x64)» → C:\\Program Files\\ACE Lab\\PC-3000\\Bin; в x86 — PC-3000 без Bin."""
    out: dict[str, tuple[str, str]] = {}

    def add(path: str, label: str) -> None:
        if os.path.isdir(path):
            out.setdefault(os.path.normcase(os.path.normpath(path)), (os.path.normpath(path), label))

    for loc, name in _installed_pc3000():
        add(os.path.join(loc, "Bin"), name)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        ace = os.path.join(base, "ACE Lab") if base else ""
        if not ace or not os.path.isdir(ace):
            continue
        for prod in _scandir(ace):
            if prod.is_dir(follow_symlinks=False):
                for b in _scandir(prod.path):
                    if b.is_dir(follow_symlinks=False) and b.name.lower() == "bin":
                        add(b.path, prod.name)
    return list(out.values())


class Index:
    """Индекс в памяти + файл рядом с агентом (переживает перезапуск). Скан — в фоне, по таймеру и по просьбе."""

    def __init__(self, state_path: Path, volumes_fn, *, skip: set[str], interval_sec: float,
                 extra_roots: list = ()):
        self.state_path = state_path
        self.volumes_fn = volumes_fn
        self.skip = skip
        self.extra_roots = list(extra_roots)
        self.interval = interval_sec
        self.lock = threading.Lock()
        self.scanning = False
        self.last_error: str | None = None
        self.wake = threading.Event()
        try:
            self.data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"scanned_at": None, "folders": [], "volumes": [], "errors": []}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.data, "scanning": self.scanning, "last_error": self.last_error,
                    "interval_sec": self.interval}

    def paths(self) -> set[str]:
        with self.lock:
            return {os.path.normcase(f["path"]) for f in self.data.get("folders", [])}

    def run_once(self) -> None:
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            data = scan(self.volumes_fn(), skip=self.skip, extra_roots=self.extra_roots)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
            with self.lock:
                self.data, self.last_error = data, None
            log.info("индекс дисков: %s папок за %s с", len(data["folders"]), data["duration_sec"])
        except Exception as e:                       # индекс не должен валить агента
            log.exception("индекс дисков не построен")
            with self.lock:
                self.last_error = str(e)[:300]
        finally:
            with self.lock:
                self.scanning = False

    def request(self) -> None:
        self.wake.set()

    def loop(self, first_delay: float = 30.0) -> None:
        self.wake.wait(first_delay)
        while True:
            self.wake.clear()
            self.run_once()
            self.wake.wait(self.interval)


# ── выгрузка ───────────────────────────────────────────────────────────────────────────────────────────────

def long_path(p: str) -> str:
    """Путь с префиксом \\\\?\\ — у восстановленных данных пути бывают длиннее 260 знаков."""
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + os.path.abspath(p)


def tree_totals(root: str) -> tuple[int, int, dict[str, tuple[int, float]]]:
    """(файлов, байт, {относительный путь: (размер, mtime)})."""
    files: dict[str, tuple[int, float]] = {}
    total = 0
    base = long_path(root)
    for dirpath, _dirs, names in os.walk(base):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files[os.path.relpath(p, base)] = (st.st_size, st.st_mtime)
            total += st.st_size
    return len(files), total, files


class Copier:
    """Одна копия за раз. Состояние — в файле рядом с агентом: бэк спрашивает ход, перезапуск агента не теряет
    историю (оборванная копия становится interrupted, повторный запуск докачивает)."""

    PROBE_FILE = "_lab_agent_write_check.txt"

    def __init__(self, state_path: Path, *, root: str, user: str = "", password: str = "", allowed_fn=None,
                 fresh_sec: float = 600):
        self.state_path = state_path
        self.root = root.rstrip("\\/")
        self.user, self.password = user, password
        self.allowed_fn = allowed_fn or (lambda: set())
        self.fresh_sec = fresh_sec
        self.lock = threading.Lock()
        self._probe: dict | None = None
        try:
            self.items: list[dict] = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = []
        for it in self.items:
            if it.get("state") in ("queued", "connecting", "counting", "copying", "verifying"):
                it["state"] = "interrupted"
        self._save()

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items[-50:], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(i) for i in self.items]

    def get(self, cid: str) -> dict | None:
        with self.lock:
            return next((dict(i) for i in self.items if i["id"] == cid), None)

    def start(self, src: str) -> tuple[dict | None, str | None]:
        """→ (запись, ошибка). Источник — только папка из индекса; цель — только `root\\<имя папки>`."""
        if not self.root:
            return None, "выгрузка выключена: в agent.ini нет delivery_root"
        src = os.path.normpath(src or "")
        if os.path.normcase(src) not in self.allowed_fn():
            return None, "папки нет в индексе дисков станции"
        name = os.path.basename(src)
        if not name or name in (".", ".."):
            return None, "у папки нет имени"
        with self.lock:
            busy = next((i for i in self.items if i["state"] in
                         ("queued", "connecting", "counting", "copying", "verifying")), None)
            if busy:
                if os.path.normcase(busy["src"]) == os.path.normcase(src):
                    return dict(busy), None
                return None, f"идёт другая выгрузка: {busy['src']}"
            rec = {"id": uuid.uuid4().hex[:12], "src": src, "dst": f"{self.root}\\{name}", "state": "queued",
                   "created_at": _iso(time.time()), "files_total": None, "bytes_total": None,
                   "files_done": 0, "bytes_done": 0, "skipped": 0, "errors": [], "verify": None}
            self.items.append(rec)
            self._save()
        threading.Thread(target=self._run, args=(rec["id"],), daemon=True).start()
        return dict(rec), None

    def _set(self, cid: str, **kw) -> None:
        with self.lock:
            for i in self.items:
                if i["id"] == cid:
                    i.update(kw)
            self._save()

    def _connect(self) -> None:
        if not (self.user and self.root.startswith("\\\\")):
            return
        parts = self.root[2:].split("\\")
        share = "\\\\" + "\\".join(parts[:2])
        r = subprocess.run(["net", "use", share, self.password, f"/user:{self.user}", "/persistent:no"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            out = (r.stdout + r.stderr).decode("cp866", "replace")
            if "1219" not in out:        # 1219 — подключение к этому серверу уже есть: работаем через него
                raise RuntimeError(f"нет доступа к шаре {share}: {out.strip()[:200]}")

    def probe(self, max_age: float = 300) -> dict:
        """Может ли агент писать в шару — из своей сессии, а не из сессии пользователя. Агент на станции работает
        задачей S4U («без входа»): у такого входа нет сетевых учётных данных, и `Z:` пользователя тут ни при чём
        (замечание станции 19.11). Пишет (перезаписывает) один свой файл в корне шары, ничего не удаляет.
        Результат кэшируется на `max_age` секунд."""
        if not self.root:
            return {"ok": False, "error": "delivery_root не задан"}
        p = self._probe
        if p and time.time() - p["_ts"] < max_age:
            return {k: v for k, v in p.items() if k != "_ts"}
        try:
            self._connect()
            target = long_path(os.path.join(self.root, self.PROBE_FILE))
            with open(target, "w", encoding="utf-8") as f:
                f.write(f"lab_agent: проверка записи {_iso(time.time())}\n")
            out = {"ok": True, "error": None}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        out.update(root=self.root, at=_iso(time.time()))
        self._probe = {**out, "_ts": time.time()}
        return out

    def probe_status(self, max_age: float = 300) -> dict:
        """Для сводки станции: не ждать сети в запросе (net use до 60 с) — последний итог, проверка в фоне."""
        p = self._probe
        if not self.root:
            return {"ok": False, "error": "delivery_root не задан"}
        if not p or time.time() - p["_ts"] >= max_age:
            if not getattr(self, "_probing", False):
                self._probing = True

                def run():
                    try:
                        self.probe(max_age=0)
                    finally:
                        self._probing = False
                threading.Thread(target=run, daemon=True, name="delivery-probe").start()
        if not p:
            return {"ok": None, "error": "проверяется", "root": self.root}
        return {k: v for k, v in p.items() if k != "_ts"}

    def _run(self, cid: str) -> None:
        rec = self.get(cid)
        src, dst = rec["src"], rec["dst"]
        try:
            self._set(cid, state="connecting", started_at=_iso(time.time()))
            self._connect()
            if not os.path.isdir(long_path(self.root)):
                raise RuntimeError(f"шара недоступна: {self.root}")
            self._set(cid, state="counting")
            n, total, files = tree_totals(src)
            newest = max((m for _s, m in files.values()), default=0)
            if self.fresh_sec and time.time() - newest < self.fresh_sec:
                raise RuntimeError(f"в папке менялись файлы {int((time.time() - newest) // 60)} мин назад — "
                                   "выгрузку в неё, похоже, ещё пишут; повторить, когда закончится")
            self._set(cid, state="copying", files_total=n, bytes_total=total)
            done = skipped = 0
            done_bytes = 0
            errors: list[str] = []
            last = time.monotonic()
            src_l, dst_l = long_path(src), long_path(dst)
            for rel, (size, mtime) in files.items():
                s, d = os.path.join(src_l, rel), os.path.join(dst_l, rel)
                try:
                    try:
                        st = os.stat(d)
                        same = st.st_size == size and abs(st.st_mtime - mtime) < 2.1
                    except OSError:
                        same = False
                    if same:
                        skipped += 1
                    else:
                        os.makedirs(os.path.dirname(d), exist_ok=True)
                        shutil.copyfile(s, d)
                        shutil.copystat(s, d)
                except OSError as e:
                    if len(errors) < 50:
                        errors.append(f"{rel}: {e}"[:300])
                    continue
                done += 1
                done_bytes += size
                if time.monotonic() - last > 5:
                    self._set(cid, files_done=done, bytes_done=done_bytes, skipped=skipped)
                    last = time.monotonic()
            self._set(cid, state="verifying", files_done=done, bytes_done=done_bytes, skipped=skipped,
                      errors=errors)
            missing = []
            dst_files = dst_bytes = 0
            for rel, (size, _m) in files.items():
                try:
                    st = os.stat(os.path.join(dst_l, rel))
                except OSError:
                    missing.append(rel)
                    continue
                if st.st_size != size:
                    missing.append(rel)
                    continue
                dst_files += 1
                dst_bytes += st.st_size
            ok = not missing and dst_files == n and dst_bytes == total
            verify = {"src_files": n, "src_bytes": total, "dst_files": dst_files, "dst_bytes": dst_bytes,
                      "ok": ok, "mismatch": missing[:50], "mismatch_total": len(missing)}
            self._set(cid, state="done" if ok else "failed", verify=verify, finished_at=_iso(time.time()),
                      error=None if ok else f"сверка не сошлась: {len(missing)} файлов не совпали")
            log.info("выгрузка %s → %s: %s (%s файлов, %s байт)", src, dst, "ок" if ok else "НЕ СОШЛАСЬ",
                     dst_files, dst_bytes)
        except Exception as e:
            log.exception("выгрузка %s → %s упала", src, dst)
            self._set(cid, state="failed", error=str(e)[:300], finished_at=_iso(time.time()))
