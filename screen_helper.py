"""Помощник снимков экрана — запускается В СЕССИИ ПОЛЬЗОВАТЕЛЯ станции, рядом с агентом.

Агент работает задачей «вне входа» (сессия 0) и экрана не видит. Помощник снимает экран сам и кладёт кадры
в папку, откуда агент их отдаёт кабинету (см. screen.py). Читает тот же agent.ini, что и агент.

    pythonw screen_helper.py --config agent.ini

Ставится один раз задачей планировщика «при входе пользователя» (интерактивно) — см. README «Снимки экрана».
Нет входа в Windows — нет и снимков: агент скажет об этом кабинету, а не отдаст старый кадр.
"""
import argparse
import configparser
import logging
import logging.handlers
import sys
from pathlib import Path

import screen

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "agent.ini"))
    args = ap.parse_args()
    parser = configparser.ConfigParser()
    if not parser.read(args.config, encoding="utf-8") or "agent" not in parser:
        sys.exit(f"Не читается конфиг агента: {args.config}")
    cfg = dict(parser["agent"])
    if cfg.get("screen_interval_sec", "300") in ("0", "off"):
        sys.exit("снимки выключены в agent.ini (screen_interval_sec = 0)")
    logfile = Path(cfg.get("screen_logfile") or HERE / "screen_helper.log")
    handlers: list[logging.Handler] = [logging.handlers.TimedRotatingFileHandler(
        logfile, when="midnight", backupCount=7, encoding="utf-8")]
    if sys.stderr is not None:                       # под pythonw консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", handlers=handlers)
    folder = Path(cfg.get("screens_dir") or HERE / "screens")
    logging.getLogger("lab_agent.screen").info("помощник снимков: %s, свежий кадр раз в %s с, лента раз в %s с",
                                               folder, cfg.get("screen_latest_sec", 30),
                                               cfg.get("screen_interval_sec", 300))
    screen.run_helper(folder,
                      latest_sec=int(cfg.get("screen_latest_sec", 30)),
                      interval=int(cfg.get("screen_interval_sec", 300)),
                      keep_hours=float(cfg.get("screen_keep_hours", 24)),
                      max_mb=int(cfg.get("screen_max_mb", 500)),
                      max_width=int(cfg.get("screen_max_width", 1600)))


if __name__ == "__main__":
    main()
