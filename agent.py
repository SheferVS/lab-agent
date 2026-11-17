"""Агент рабочей станции восстановления данных.

Ставится на машину лаборатории, где лежат образы и восстановленные файлы.
Слушает нестандартный порт по TLS и отдаёт бэкенду дерево папок и файлы —
чтобы клиент в веб-просмотрщике видел свои данные, физически лежащие здесь.

Только стандартная библиотека: на лабораторной машине не должно быть возни с
виртуальными окружениями и pip. Python 3.10+ (аннотации `Path | None`).

    python agent.py --config agent.ini

Защита (три независимых слоя, см. README.md):
  1. bearer-токен — без него любой запрос 401;
  2. TLS с самоподписанным сертификатом, бэкенд пиннит его отпечаток;
  3. allowlist IP — принимаем только с адреса нашего сервера.

Наружу отдаётся ТОЛЬКО то, что лежит внутри разрешённых корней (`data_roots`).
Любой путь нормализуется и проверяется на принадлежность корню, поэтому `..`,
симлинки и абсолютные пути мимо корней не работают.

Задания (`/jobs…`, ТЗ TZ_lab_station_logical_recovery_2026-11-17.md §5, §5а) — только если в
конфиге задан `jobs_dir`. Без него ручки отвечают 404 и агент ведёт себя как раньше.
Агент заданий НЕ исполняет и ничего не запускает: пишущие ручки только кладут файлы в
`jobs_dir/<job_id>/`, исполнитель — отдельный процесс станции.
  job.json       — задание (POST /jobs); повтор того же job_id — no-op;
  answers.jsonl  — ответы на вопросы станции (дубль по question_id — no-op);
  notes.jsonl    — сообщения сотрудника сессии (§5а; дубль по note_id — no-op);
  selection.roa  — отметки клиента (атомарная замена);
  cancel         — отмена;
  events.jsonl   — пишет ТОЛЬКО исполнитель, агент читает (GET /jobs/<id>?since=);
  state.json     — {"state": …}, пишет исполнитель;
  прочее         — промежуточные файлы исполнителя: GET /jobs/<id>/file?name=&offset=
                   (только из папки задания, `data_roots` расширять не нужно).

1.2 (ТЗ §10б п.2 — копия задания у нас): GET /jobs — список заданий, GET /jobs/<id>/files — опись
папки, `offset` у /file — докачка с места, GET /station — пульс исполнителя, тома, хвосты журналов.
Всё только чтение.
"""

import argparse
import configparser
import hmac
import json
import logging
import logging.handlers
import os
import re
import shutil
import ssl
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Встраиваемый Python на станции (python312._pth) не кладёт папку скрипта в sys.path — без этой строки
# `import screen` падает ModuleNotFoundError (LAB-VLAD 18.11 ~04:35; runner.py лечится так же).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("lab_agent")

CONFIG: dict = {}
# Файлы отдаём кусками — образы и видео не должны попадать в память целиком.
CHUNK = 1024 * 1024

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_JSON_BODY = 1024 * 1024              # задание, ответ, сообщение
MAX_SELECTION_BODY = 64 * 1024 * 1024    # .roa на сотни тысяч файлов
MAX_EVENTS_PER_REPLY = 1000
MAX_TEXT = 20_000
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_JOB_FILES = 20_000
VOLUME_TIMEOUT = 5.0
LOG_TAIL_BYTES = 256 * 1024
MAX_LOG_LINES = 500
_APPEND_LOCK = threading.Lock()

VERSION = "1.3"


def git_commit() -> str:
    """Коммит, из которого запущен агент (станция обновляется сама из открытого репозитория — updater.py).
    Файл REVISION пишет обновлятор; иначе — .git/HEAD рабочей копии. Ничего нет — пусто."""
    here = Path(__file__).resolve().parent
    try:
        return (here / "REVISION").read_text(encoding="utf-8").strip()[:12]
    except OSError:
        pass
    git = here / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12]
        ref = head[4:].strip()
        if (git / ref).exists():
            return (git / ref).read_text(encoding="utf-8").strip()[:12]
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        pass
    return ""


def screens_dir() -> Path:
    return Path(CONFIG.get("screens_dir") or Path(__file__).resolve().with_name("screens"))


def _roots() -> list[Path]:
    out = []
    for raw in CONFIG.get("data_roots", "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(Path(raw).resolve())
        except Exception as e:
            log.warning("нерабочий корень %s: %s", raw, e)
    return out


def safe_path(raw: str) -> Path | None:
    """Путь внутри разрешённых корней либо None.

    resolve() снимает `..` и раскрывает симлинки — сравнение уже настоящих путей.
    """
    if not raw:
        return None
    try:
        target = Path(raw).resolve()
    except Exception:
        return None
    for root in _roots():
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    return None


# ── задания: файлы ──

def jobs_root() -> Path | None:
    raw = (CONFIG.get("jobs_dir") or "").strip()
    return Path(raw).resolve() if raw else None


def job_dir(job_id: str) -> Path | None:
    """Папка задания внутри `jobs_dir` или None (нет jobs_dir / кривой id)."""
    root = jobs_root()
    if root is None or not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        return None
    d = (root / job_id).resolve()
    try:
        d.relative_to(root)
    except ValueError:
        return None
    return d


def write_atomic(path: Path, data: bytes) -> None:
    """Записать целиком или никак: исполнитель не увидит недописанный файл."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # недописанная последняя строка исполнителя — пропускаем, на следующем опросе будет целой
                log.warning("битая строка в %s: %r", path, line[:120])
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def append_once(path: Path, key: str, record: dict) -> bool:
    """Дописать строку, если записи с тем же `key` ещё нет. True — дописали."""
    with _APPEND_LOCK:
        if any(r.get(key) == record[key] for r in read_jsonl(path)):
            return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True


def job_status(d: Path, since: int) -> dict:
    """Состояние задания и события с seq > since (не больше MAX_EVENTS_PER_REPLY)."""
    state = "queued"
    st_file = d / "state.json"
    if st_file.exists():
        try:
            state = json.loads(st_file.read_text(encoding="utf-8")).get("state") or state
        except (ValueError, AttributeError):
            pass
    if (d / "cancel").exists() and state in ("queued", "running", "waiting_answer", "waiting_selection"):
        state = "cancel_requested"
    events = [e for e in read_jsonl(d / "events.jsonl")
              if isinstance(e.get("seq"), int) and e["seq"] > since]
    events.sort(key=lambda e: e["seq"])
    return {"job_id": d.name, "state": state,
            "events": events[:MAX_EVENTS_PER_REPLY], "more": len(events) > MAX_EVENTS_PER_REPLY}


def _mtime_iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_jobs() -> list[dict]:
    """Все задания `jobs_dir`: только папки с job.json (рядом лежит `_runner.json` и прочее).

    Сервер по этому списку находит и задания, заведённые на станции руками (ТЗ §10б п.2).
    """
    root = jobs_root()
    if root is None or not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not JOB_ID_RE.match(d.name) or not (d / "job.json").is_file():
            continue
        st = job_status(d, 0)
        events = st["events"]
        mt = [p.stat().st_mtime for p in (d / "job.json", d / "state.json", d / "events.jsonl") if p.exists()]
        order_ref = None
        try:
            order_ref = json.loads((d / "job.json").read_text(encoding="utf-8")).get("order_ref")
        except (ValueError, OSError, AttributeError):
            pass
        out.append({"job_id": d.name, "order_ref": order_ref, "state": st["state"],
                    "last_seq": max((e["seq"] for e in events), default=0),
                    "updated_at": _mtime_iso(max(mt, default=None))})
    return out


def list_job_files(d: Path, max_entries: int = MAX_JOB_FILES) -> dict:
    """Опись папки задания: путь от папки (через «/»), размер, mtime. Недописанные `.tmp` не отдаём."""
    files, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".tmp"):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"name": p.relative_to(d).as_posix(), "size": st.st_size,
                          "mtime": _mtime_iso(st.st_mtime)})
            if len(files) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    return {"job_id": d.name, "files": files, "count": len(files), "truncated": truncated}


def runner_pulse() -> dict | None:
    """`jobs_dir/_runner.json` исполнителя (он перезаписывает его каждый проход) + жив ли он.

    Мёртв — если `at` старше трёх `poll_seconds` (договорённость со станцией 17.11).
    """
    root = jobs_root()
    if root is None:
        return None
    p = root / "_runner.json"
    if not p.is_file():
        return {"present": False, "alive": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"present": True, "alive": False, "error": "не читается"}
    if not isinstance(data, dict):
        return {"present": True, "alive": False, "error": "не объект"}
    alive, age = False, None
    try:
        at = datetime.strptime(str(data.get("at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - at).total_seconds()
        alive = age <= 3 * float(data.get("poll_seconds") or 60)
    except (ValueError, TypeError):
        pass
    return {"present": True, "alive": alive, "age_sec": None if age is None else int(age), **data}


def _volume_info(letter: str) -> dict:
    import ctypes
    k32 = ctypes.windll.kernel32
    root = f"{letter}:\\"
    kinds = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
    info = {"letter": letter, "type": kinds.get(k32.GetDriveTypeW(root), "unknown")}
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    if k32.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), None, None, fs, 261):
        info.update(label=label.value, fs=fs.value, volume_serial=f"{serial.value:08X}")
        try:
            du = shutil.disk_usage(root)
            info.update(total=du.total, free=du.free)
        except OSError:
            pass
    else:
        info["ready"] = False
    return info


def list_volumes(timeout: float = VOLUME_TIMEOUT) -> list[dict]:
    """Тома Windows: буква, метка, ФС, объём, свободно.

    Каждый том — в своём потоке с пределом: отвалившийся сетевой или USB-том (M: 17.11 15:42)
    может держать вызов долго, а сводка станции не должна от этого висеть.
    """
    if os.name != "nt":
        return []
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(ord("A") + i)
        box: dict = {}
        t = threading.Thread(target=lambda l=letter: box.update(_volume_info(l)), daemon=True)
        t.start()
        t.join(timeout)
        out.append(box if box else {"letter": letter, "ready": False, "error": "timeout"})
    return out


def log_tails(lines: int) -> list[dict]:
    """Хвосты `*.log` из папки журнала агента (там же журнал исполнителя). Только эта папка."""
    logfile = CONFIG.get("logfile")
    if not logfile:
        return []
    folder = Path(logfile).resolve().parent
    out = []
    for p in sorted(folder.glob("*.log")):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace").splitlines()[-lines:]
            out.append({"name": p.name, "size": size, "mtime": _mtime_iso(p.stat().st_mtime), "tail": tail})
        except OSError as e:
            out.append({"name": p.name, "error": str(e)[:200]})
    return out


def scan_tree(base: Path, max_entries: int = 200_000) -> dict:
    """Плоский список файлов под base: путь, размер, mtime."""
    files, total, truncated = [], 0, False
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith("$")]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "path": str(p),
                "rel": str(p.relative_to(base)),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
            total += st.st_size
            if len(files) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    return {"base": str(base), "files": files, "count": len(files),
            "bytes": total, "truncated": truncated}


class Handler(BaseHTTPRequestHandler):
    server_version = f"LabAgent/{VERSION}"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ── проверки ──

    def _client_allowed(self) -> bool:
        allow = [a.strip() for a in CONFIG.get("allow_ips", "").split(",") if a.strip()]
        if not allow:
            return True  # пусто = без ограничения (см. README, так делать не надо)
        return self.client_address[0] in allow

    def _token_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        # compare_digest — чтобы по времени ответа нельзя было подбирать токен
        return hmac.compare_digest(header[7:].strip(), CONFIG.get("token", ""))

    def _guard(self, loopback_ok: bool = False) -> bool:
        # /health с этой же машины — без списка IP (токен всё равно нужен): обновлятор проверяет агента
        # после установки на 127.0.0.1, а allow_ips на станции — только адрес сервера (LAB-VLAD 18.11 ~04:35:
        # иначе любое автообновление кончалось бы откатом).
        local = loopback_ok and self.client_address[0] in ("127.0.0.1", "::1")
        if not local and not self._client_allowed():
            log.warning("отказ по IP: %s", self.client_address[0])
            self._json({"error": "forbidden"}, 403)
            return False
        if not self._token_ok():
            log.warning("отказ по токену: %s", self.client_address[0])
            self._json({"error": "unauthorized"}, 401)
            return False
        return True

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── маршруты ──

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        if not self._guard(loopback_ok=(route == "/health")):
            return

        try:
            if route == "/health":
                roots = [{"path": str(r), "exists": r.exists()} for r in _roots()]
                jr = jobs_root()
                self._json({"ok": True, "name": CONFIG.get("name", ""), "version": VERSION, "roots": roots,
                            "jobs": bool(jr) and jr.exists(), "commit": git_commit()})

            elif route == "/station":
                try:
                    lines = max(0, min(MAX_LOG_LINES, int((q.get("lines") or ["50"])[0])))
                except ValueError:
                    self._json({"error": "lines must be int"}, 400)
                    return
                self._json({"name": CONFIG.get("name", ""), "agent_version": VERSION,
                            "now": _mtime_iso(datetime.now(timezone.utc).timestamp()),
                            "runner": runner_pulse(), "volumes": list_volumes(),
                            "logs": log_tails(lines)})

            elif route == "/jobs":
                if jobs_root() is None:
                    self._json({"error": "jobs disabled (no jobs_dir)"}, 404)
                    return
                self._json({"jobs": list_jobs(), "runner": runner_pulse()})

            elif route == "/order-dir":
                num = (q.get("num") or [""])[0].strip()
                if not num:
                    self._json({"error": "num required"}, 400)
                    return
                self._json(self._find_order_dir(num))

            elif route == "/tree":
                target = safe_path((q.get("path") or [""])[0])
                if not target or not target.is_dir():
                    self._json({"error": "path not allowed or not a directory"}, 400)
                    return
                self._json(scan_tree(target))

            elif route == "/file":
                self._send_file(safe_path((q.get("path") or [""])[0]))

            elif route.startswith("/jobs/"):
                self._get_job(route, q)

            elif route == "/screenshot":
                # без name — свежий кадр помощника из сессии пользователя; с name — кадр из ленты
                self._send_screen((q.get("name") or [""])[0])

            elif route == "/screenshots":
                import screen
                rec = screen.Recorder(screens_dir())
                since = float((q.get("since") or ["0"])[0] or 0)
                try:
                    _, age = screen.read_latest(screens_dir(), float(CONFIG.get("screen_max_age_sec", 120)))
                    latest = {"ok": True, "age_sec": int(age)}
                except screen.ScreenError as e:
                    latest = {"ok": False, "error": str(e)}
                self._json({"enabled": True, "interval_sec": int(CONFIG.get("screen_interval_sec", 300)),
                            "latest": latest, "last_error": None if latest["ok"] else latest["error"],
                            "items": rec.listing(since)})

            else:
                self._json({"error": "not found"}, 404)

        except Exception as e:
            log.exception("ошибка обработки %s", route)
            self._json({"error": str(e)[:200]}, 500)

    def do_POST(self):
        if not self._guard():
            return
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if route == "/jobs":
                self._post_create_job()
            elif route.startswith("/jobs/"):
                self._post_job_action(route)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("ошибка обработки POST %s", route)
            self._json({"error": str(e)[:200]}, 500)

    # ── задания (§5, §5а) ──

    def _job_from_route(self, route: str, action: str = "") -> Path | None:
        """`/jobs/<id>[/<action>]` → папка существующего задания, иначе ответ 404 и None."""
        parts = route.split("/")  # ['', 'jobs', '<id>', <action>?]
        if len(parts) != (4 if action else 3) or (action and parts[3] != action):
            self._json({"error": "not found"}, 404)
            return None
        if jobs_root() is None:
            self._json({"error": "jobs disabled (no jobs_dir)"}, 404)
            return None
        d = job_dir(parts[2])
        if d is None or not (d / "job.json").is_file():
            self._json({"error": "job not found"}, 404)
            return None
        return d

    def _get_job(self, route: str, q: dict):
        if route.endswith("/files"):
            d = self._job_from_route(route, "files")
            if d is not None:
                self._json(list_job_files(d))
            return
        if route.endswith("/file"):
            d = self._job_from_route(route, "file")
            if d is None:
                return
            name = (q.get("name") or [""])[0]
            target = None
            if name:
                try:
                    cand = (d / name).resolve()
                    cand.relative_to(d)
                    target = cand
                except (ValueError, OSError):
                    target = None
            try:
                offset = int((q.get("offset") or ["0"])[0])
            except ValueError:
                offset = -1
            if offset < 0:
                self._json({"error": "offset must be int >= 0"}, 400)
                return
            self._send_file(target, offset)
            return
        d = self._job_from_route(route)
        if d is None:
            return
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            self._json({"error": "since must be int"}, 400)
            return
        self._json(job_status(d, since))

    def _read_body(self, limit: int) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json({"error": "Content-Length required"}, 411)
            return None
        if n < 0 or n > limit:
            self._json({"error": f"body too large (> {limit})"}, 413)
            return None
        return self.rfile.read(n)

    def _read_json(self) -> dict | None:
        raw = self._read_body(MAX_JSON_BODY)
        if raw is None:
            return None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "bad json"}, 400)
            return None
        if not isinstance(body, dict):
            self._json({"error": "json object expected"}, 400)
            return None
        return body

    def _post_create_job(self):
        root = jobs_root()
        if root is None:
            self._json({"error": "jobs disabled (no jobs_dir)"}, 404)
            return
        body = self._read_json()
        if body is None:
            return
        job_id = body.get("job_id")
        d = job_dir(job_id)
        if d is None:
            self._json({"error": "job_id required ([A-Za-z0-9_-]{1,64})"}, 400)
            return
        root.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir()
        except FileExistsError:
            # Повтор (сервер не дождался ответа) — no-op, содержимое не трогаем.
            self._json({"ok": True, "job_id": job_id, "existed": True})
            return
        write_atomic(d / "job.json", json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8"))
        log.info("задание %s принято (заказ %s)", job_id, body.get("order_ref"))
        self._json({"ok": True, "job_id": job_id, "existed": False})

    def _post_job_action(self, route: str):
        parts = route.split("/")
        action = parts[3] if len(parts) == 4 else ""
        if action not in ("answer", "note", "selection", "cancel"):
            self._json({"error": "not found"}, 404)
            return
        d = self._job_from_route(route, action)
        if d is None:
            return

        if action == "selection":
            raw = self._read_body(MAX_SELECTION_BODY)
            if raw is None:
                return
            if not raw.startswith(SQLITE_MAGIC):
                self._json({"error": ".roa (SQLite) expected"}, 400)
                return
            write_atomic(d / "selection.roa", raw)
            log.info("задание %s: отметки клиента, %d Б", d.name, len(raw))
            self._json({"ok": True, "bytes": len(raw)})
            return

        body = self._read_json()
        if body is None:
            return

        if action == "cancel":
            rec = {"reason": str(body.get("reason") or "")[:MAX_TEXT],
                   "from": str(body.get("from") or "")[:200]}
            write_atomic(d / "cancel", json.dumps(rec, ensure_ascii=False).encode("utf-8"))
            log.info("задание %s: отмена (%s)", d.name, rec["reason"][:80])
            self._json({"ok": True})
            return

        key = "question_id" if action == "answer" else "note_id"
        rid, text = body.get(key), body.get("text")
        if not isinstance(rid, str) or not JOB_ID_RE.match(rid):
            self._json({"error": f"{key} required ([A-Za-z0-9_-]{{1,64}})"}, 400)
            return
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            self._json({"error": f"text required, <= {MAX_TEXT} chars"}, 400)
            return
        rec = {key: rid, "text": text, "from": str(body.get("from") or "")[:200],
               "at": str(body.get("at") or "")[:40]}
        added = append_once(d / ("answers.jsonl" if action == "answer" else "notes.jsonl"), key, rec)
        log.info("задание %s: %s %s от %s%s", d.name, action, rid, rec["from"], "" if added else " (повтор)")
        self._json({"ok": True, "duplicate": not added})

    def _find_order_dir(self, num: str) -> dict:
        """Папка заказа внутри корней: `45685`, `45685-1`, `45685_data`."""
        best = None
        for root in _roots():
            if not root.is_dir():
                continue
            try:
                for child in root.iterdir():
                    if not child.is_dir() or not child.name.startswith(num):
                        continue
                    st = child.stat()
                    if best is None or st.st_mtime > best[1]:
                        best = (child, st.st_mtime)
            except OSError as e:
                log.warning("не читается корень %s: %s", root, e)
        if not best:
            return {"found": False, "num": num}
        return {"found": True, "num": num, "path": str(best[0])}

    def _send_screen(self, name: str):
        """Кадр экрана. Агент сам не снимает (он в сессии 0) — отдаёт то, что записал screen_helper.py."""
        import screen
        if name:
            target = screen.Recorder(screens_dir()).path(name)
            if not target:
                self._json({"error": "no such screenshot"}, 404)
                return
            body, age = target.read_bytes(), None
        else:
            try:
                body, age = screen.read_latest(screens_dir(), float(CONFIG.get("screen_max_age_sec", 120)))
            except screen.ScreenError as e:
                self._json({"error": str(e)}, 409)
                return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if age is not None:
            self.send_header("X-Screen-Age", str(int(age)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path | None, offset: int = 0):
        """Файл потоком. `offset` — докачка растущего файла (переписка, журнал) с места:
        отдаём байты [offset, size), полный размер — в X-File-Size. offset > size — 416
        (файл перезаписали короче — сервер качает заново с нуля)."""
        if not target or not target.is_file():
            self._json({"error": "path not allowed or not a file"}, 400)
            return
        with open(target, "rb") as f:
            # размер по открытому файлу: строка, дописанная после stat, не порвёт Content-Length
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if offset > size:
                self._json({"error": "offset beyond end", "size": size}, 416)
                return
            f.seek(offset)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size - offset))
            self.send_header("X-File-Size", str(size))
            self.send_header("X-Offset", str(offset))
            self.end_headers()
            left = size - offset
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def load_config(path: str) -> dict:
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding="utf-8"):
        sys.exit(f"Не читается конфиг: {path}")
    if "agent" not in parser:
        sys.exit("В конфиге нет секции [agent]")
    cfg = dict(parser["agent"])
    for required in ("token", "data_roots", "certfile", "keyfile"):
        if not cfg.get(required):
            sys.exit(f"В конфиге не задан {required}")
    if len(cfg["token"]) < 32:
        sys.exit("Токен короче 32 символов — сгенерируйте длиннее (см. README)")
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="agent.ini")
    args = ap.parse_args()

    global CONFIG
    CONFIG = load_config(args.config)

    # Агент живёт задачей планировщика под pythonw: консоли нет (sys.stderr = None), журнал —
    # только файл, по суткам, 14 дней (logfile_days), иначе за полгода вырастет без предела.
    logfile = CONFIG.get("logfile", "lab_agent.log")
    Path(logfile).resolve().parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.TimedRotatingFileHandler(
        logfile, when="midnight", backupCount=int(CONFIG.get("logfile_days", 14)), encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=handlers,
    )

    port = int(CONFIG.get("port", 0))
    if not port:
        sys.exit("В конфиге не задан port")

    missing = [str(r) for r in _roots() if not r.exists()]
    if missing:
        log.warning("корни не найдены (диск не подключён?): %s", ", ".join(missing))

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CONFIG["certfile"], keyfile=CONFIG["keyfile"])
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    allow = CONFIG.get("allow_ips", "")
    log.info("агент «%s» слушает :%d | корни: %s | allow_ips: %s | задания: %s",
             CONFIG.get("name", "?"), port, CONFIG.get("data_roots"),
             allow or "ВСЕ (небезопасно)", CONFIG.get("jobs_dir") or "выключены (нет jobs_dir)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен вручную")


if __name__ == "__main__":
    main()
