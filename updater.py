"""Обновлятор агента станции: сам ставит новую версию, как только её выложили в открытый репозиторий.

Виктор 18.11.2026 ~04:30: «настрой чтобы он сам обновлялся с гита как только запушили»; ~04:35 «агента можем
положить в открытый репо же? а енв ток локально». Репозиторий — только код (agent.py, screen.py, …);
agent.ini, сертификат, ключ, снимки и журналы живут только на станции и обновлятором не трогаются.

    python updater.py --config agent.ini            # один проход (его и зовёт планировщик раз в 5 минут)
    python updater.py --config agent.ini --check    # только сказать, есть ли новая версия

Проход:
  1. спросить GitHub, какой коммит сейчас в ветке (`[update] repo`, `branch`; без git и без ключей —
     репозиторий открытый);
  2. тот же, что стоит, или уже признан плохим — выйти;
  3. скачать архив ИМЕННО этого коммита, взять только файлы из белого списка, проверить, что все .py
     компилируются;
  4. сохранить текущие файлы в `releases/prev`, разложить новые, записать REVISION;
  5. перезапустить задачи планировщика агента (и помощника снимков, если задан) и спросить у агента /health;
  6. агент не поднялся или версия не та — вернуть прежние файлы, перезапустить, коммит записать в плохие.
Состояние и история — `updater_state.json`, журнал — `updater.log`. Только стандартная библиотека.

Доверие: станция выполняет любой код, попавший в ветку репозитория. Писать в неё должен только владелец
(защита ветки + двухфакторный вход на GitHub) — см. README.
"""
import argparse
import configparser
import io
import json
import logging
import logging.handlers
import py_compile
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: Что обновлятор вообще может положить в папку агента. Всё остальное в архиве игнорируется, и больше ничего
#: в папке он не трогает (на LAB-VLAD в ней же живут runner.py, lab_jobs.py, de_watch.py, свой README):
#: свои файлы + REVISION, updater_state.json, updater.log, releases/prev.
FILES = ("agent.py", "screen.py", "screen_helper.py", "updater.py", "datapool.py")
log = logging.getLogger("lab_agent.updater")


def _get(url: str, timeout: float = 30, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lab-agent-updater",
                                               **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_sha(repo: str, branch: str) -> str:
    return _get(f"https://api.github.com/repos/{repo}/commits/{branch}",
                accept="application/vnd.github.sha").decode().strip()


#: Коммиттер, которым GitHub подписывает слияние PR и правку через сайт — автор при этом настоящий.
WEB_FLOW = "web-flow"


def commit_authors(repo: str, sha: str) -> tuple[str, str]:
    """(логин автора, логин коммиттера) на GitHub; неизвестный GitHub-у — пустая строка."""
    d = json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{sha}"))
    return tuple((x or {}).get("login") or "" for x in (d.get("author"), d.get("committer")))


def author_ok(authors: tuple[str, str], allowed: set[str]) -> bool:
    """Автор — из разрешённых; коммиттер — из разрешённых или web-flow (слияние PR на сайте, LAB-VLAD 18.11)."""
    author, committer = authors
    return author in allowed and (committer in allowed or committer == WEB_FLOW)


def fetch_release(repo: str, sha: str) -> dict[str, bytes]:
    """Файлы белого списка из архива коммита sha. Нет agent.py — архив не наш, отказ."""
    data = _get(f"https://codeload.github.com/{repo}/zip/{sha}", timeout=120)
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            parts = name.split("/", 1)                    # «lab-agent-<sha>/agent.py»
            if len(parts) == 2 and parts[1] in FILES:
                out[parts[1]] = z.read(name)
    if "agent.py" not in out:
        raise RuntimeError(f"в архиве {sha[:12]} нет agent.py")
    return out


def compiles(files: dict[str, bytes]) -> None:
    with tempfile.TemporaryDirectory() as d:
        for name, body in files.items():
            if name.endswith(".py"):
                p = Path(d) / name
                p.write_bytes(body)
                py_compile.compile(str(p), doraise=True)


def install(files: dict[str, bytes], sha: str) -> Path:
    """Текущие файлы — в releases/prev, новые — на место (каждый атомарно), REVISION = sha."""
    prev = HERE / "releases" / "prev"
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir(parents=True)
    for name in FILES + ("REVISION",):
        if (HERE / name).exists():
            shutil.copy2(HERE / name, prev / name)
    for name, body in files.items():
        tmp = HERE / (name + ".new")
        tmp.write_bytes(body)
        tmp.replace(HERE / name)
    (HERE / "REVISION").write_text(sha, encoding="utf-8")
    return prev


def rollback(prev: Path) -> None:
    for f in prev.iterdir():
        shutil.copy2(f, HERE / f.name)
    if not (prev / "REVISION").exists():
        (HERE / "REVISION").unlink(missing_ok=True)


def restart(task: str) -> None:
    if not task:
        return
    subprocess.run(["schtasks", "/End", "/TN", task], capture_output=True)
    time.sleep(2)
    r = subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /Run {task}: {(r.stdout or r.stderr).strip()[:200]}")


def health(cfg: dict, wait: float = 60) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                       # свой же агент на 127.0.0.1, сертификат самоподписанный
    url = f"https://127.0.0.1:{cfg['port']}/health"
    end, last = time.time() + wait, None
    while time.time() < end:
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + cfg["token"]})
            with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"агент не ответил на /health за {int(wait)} с: {last}")


def load_state() -> dict:
    try:
        return json.loads((HERE / "updater_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    st["history"] = st.get("history", [])[-50:]
    (HERE / "updater_state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def run_once(cfg: dict, up: dict, *, check_only: bool = False) -> str:
    st = load_state()
    st["last_check"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        sha = remote_sha(up["repo"], up.get("branch", "main"))
    except Exception as e:
        st["last_error"] = f"GitHub недоступен: {e}"
        save_state(st)
        return st["last_error"]
    current = (HERE / "REVISION").read_text(encoding="utf-8").strip() if (HERE / "REVISION").exists() else ""
    # Прежний обновлятор ставит только свой список файлов: новый файл (datapool.py в 1.4) приезжает вторым
    # проходом уже нового обновлятора — та же версия, но файла нет → ставим её заново. Один раз на версию
    # (`healed`): если файла нет и в самом релизе, переустановка каждые 5 минут перезапускала бы агента.
    missing = [f for f in FILES if not (HERE / f).exists()]
    if sha == current and missing and st.get("healed") != sha:
        current = ""
        log.info("стоит %s, но нет %s — ставлю версию заново", sha[:12], ", ".join(missing))
    if sha == current:
        st["last_error"] = None
        save_state(st)
        return f"стоит последняя версия {sha[:12]}"
    if sha in st.get("bad", []):
        save_state(st)
        return f"{sha[:12]} уже откатывали — ждём следующий коммит"
    # auto = false — только сообщить (в журнал и updater_state.json), ставит человек: python updater.py
    auto = str(up.get("auto", "true")).strip().lower() not in ("0", "false", "no", "off")
    if check_only or not auto:
        st["available"] = sha
        save_state(st)
        return f"есть новая версия {sha[:12]} (стоит {current[:12] or '—'})" + ("" if auto else "; auto = false — не ставлю")
    try:
        allowed = {a.strip() for a in str(up.get("allowed_authors", "SheferVS")).split(",") if a.strip()}
        who = commit_authors(up["repo"], sha)
        if not author_ok(who, allowed):
            raise RuntimeError(f"коммит: автор {who[0] or '?'}, коммиттер {who[1] or '?'}; "
                               f"разрешены: {', '.join(sorted(allowed))}")
        files = fetch_release(up["repo"], sha)
        compiles(files)
    except Exception as e:
        st.setdefault("bad", []).append(sha)
        st["last_error"] = f"{sha[:12]} не взят: {e}"
        st.setdefault("history", []).append({"at": st["last_check"], "sha": sha, "result": "rejected", "error": str(e)})
        save_state(st)
        return st["last_error"]
    prev = install(files, sha)
    try:
        restart(up.get("agent_task", ""))
        restart(up.get("helper_task", ""))
        h = health(cfg)
        if not h.get("ok") or h.get("commit", "")[:12] != sha[:12]:
            raise RuntimeError(f"агент поднялся не той версией: {h.get('version')} {h.get('commit')}")
    except Exception as e:
        log.error("обновление %s не встало (%s) — откат на %s", sha[:12], e, current[:12] or "прежние файлы")
        rollback(prev)
        try:
            restart(up.get("agent_task", ""))
            restart(up.get("helper_task", ""))
        except Exception as e2:
            log.error("перезапуск после отката: %s", e2)
        st.setdefault("bad", []).append(sha)
        st["last_error"] = f"{sha[:12]} откатили: {e}"
        st.setdefault("history", []).append({"at": st["last_check"], "sha": sha, "result": "rolled_back", "error": str(e)})
        save_state(st)
        return st["last_error"]
    st.update(current=sha, last_error=None, installed_at=st["last_check"], version=h.get("version"), healed=sha)
    st.setdefault("history", []).append({"at": st["last_check"], "sha": sha, "result": "installed",
                                         "version": h.get("version")})
    save_state(st)
    return f"установлена {h.get('version')} ({sha[:12]})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "agent.ini"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    parser = configparser.ConfigParser()
    if not parser.read(args.config, encoding="utf-8") or "agent" not in parser:
        sys.exit(f"Не читается конфиг агента: {args.config}")
    cfg = dict(parser["agent"])
    up = dict(parser["update"]) if "update" in parser else {}
    up.setdefault("repo", "SheferVS/lab-agent")
    handlers: list[logging.Handler] = [logging.handlers.TimedRotatingFileHandler(
        HERE / "updater.log", when="midnight", backupCount=14, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", handlers=handlers)
    log.info(run_once(cfg, up, check_only=args.check))


if __name__ == "__main__":
    main()
