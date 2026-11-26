"""Размеры папок и корзина станции (агент 1.5, #4131, ТЗ §3.2): python -m pytest tools/lab_agent/test_datapool_folders.py

Папки настоящие (временный каталог pytest): переименование, удаление и запреты проверять подделкой файловой
системы бессмысленно — ломается именно она. Чего тесты НЕ доказывают: что на боевом томе станции с идущим
снятием запреты срабатывают. Это проверяется на папке-пустышке на самой станции (ТЗ §3.6).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datapool as D  # noqa: E402


@pytest.fixture(autouse=True)
def _not_system_drive(monkeypatch):
    """`tmp_path` у pytest лежит на системном томе, а корзина системный том не трогает намеренно. Чтобы
    проверять ОСТАЛЬНЫЕ запреты, на время теста говорим, что системный том — другая буква; сам запрет
    системного тома проверяется отдельным тестом с настоящей буквой."""
    monkeypatch.setenv("SystemDrive", "Q:")


def _folder(tmp_path, name, files=(("a.bin", 100), ("b.bin", 200))):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, size in files:
        (d / fn).write_bytes(b"x" * size)
    return d


def _index_rec(path, *, newest=None):
    """Запись индекса о папке — как её отдаёт datapool.describe."""
    return {"path": str(path), "name": os.path.basename(str(path)), "volume": os.path.splitdrive(str(path))[0],
            "mtime": D._iso(os.stat(path).st_mtime), "newest_file_at": newest, "order_nums": [], "images": []}


# ── размеры ──

def test_dir_size_counts_files_and_bytes(tmp_path):
    d = _folder(tmp_path, "46154_data")
    (d / "sub").mkdir()
    (d / "sub" / "c.bin").write_bytes(b"y" * 50)
    res = D.dir_size(str(d), pause_each=0)
    assert res == {"files_total": 3, "bytes_total": 350, "partial": False, "error": None}


def test_dir_size_stops_on_deadline(tmp_path):
    """Предел времени отдаёт частичный ответ, а не ноль и не None: «посчитали столько» видно по partial."""
    d = _folder(tmp_path, "46155", files=[(f"f{i}.bin", 10) for i in range(6)])
    res = D.dir_size(str(d), pause_each=1, pause_sec=0.01, max_sec=1e-9)
    assert res["partial"] is True
    # сколько именно успели — не проверяем: у time.monotonic() на Windows шаг ~15 мс, и число файлов до
    # первой сработавшей отсечки от этого плавает. Важно, что счёт прервался и это видно по partial
    assert 0 < res["files_total"] < 6 and res["bytes_total"] == res["files_total"] * 10
    # 0 и None — «считать до конца», а не «предел нулевой»: иначе настройка size_max_min = 0 тихо
    # превратила бы все размеры в частичные
    assert D.dir_size(str(d), pause_each=1, pause_sec=0, max_sec=0)["partial"] is False


def test_sizer_counts_one_folder_per_pass_and_recounts_after_change(tmp_path):
    d1, d2 = _folder(tmp_path, "46156"), _folder(tmp_path, "46157")
    folders = [_index_rec(d1), _index_rec(d2)]
    sizer = D.Sizer(tmp_path / "sizes.json", lambda: folders)
    assert len(sizer.stale_folders()) == 2
    assert sizer.run_once()["path"] == str(d1)        # за проход — одна папка, между ними пауза
    assert len(sizer.stale_folders()) == 1
    sizer.run_once()
    assert sizer.stale_folders() == []
    # в папку доложили файл, и индекс на следующем проходе отдал новое время папки → пересчёт.
    # Время в индексе с точностью до секунды, поэтому в тесте подставляем его явно, а не ждём тика часов
    (d1 / "new.bin").write_bytes(b"z" * 7)
    folders[0] = {**_index_rec(d1), "mtime": "2030-01-01T00:00:00Z"}
    assert [f["path"] for f in sizer.stale_folders()] == [str(d1)]
    assert sizer.run_once()["bytes_total"] == 307
    assert json.loads((tmp_path / "sizes.json").read_text(encoding="utf-8"))["sizes"]


def test_sizer_forgets_folders_gone_from_index(tmp_path):
    d = _folder(tmp_path, "46158")
    folders = [_index_rec(d)]
    sizer = D.Sizer(tmp_path / "sizes.json", lambda: folders)
    sizer.run_once()
    folders.clear()
    sizer.run_once()
    assert sizer.all() == {}


def test_merge_sizes_marks_uncounted_as_null(tmp_path):
    d1, d2 = _folder(tmp_path, "46159"), _folder(tmp_path, "46160")
    sizer = D.Sizer(tmp_path / "sizes.json", lambda: [_index_rec(d1)])
    sizer.run_once()
    out = D.merge_sizes([_index_rec(d1), _index_rec(d2)], sizer.all())
    assert out[0]["bytes_total"] == 300 and out[0]["sized_at"]
    # не посчитано — null, а не ноль: по этому числу человек решает, удалять ли
    assert out[1]["bytes_total"] is None and out[1]["files_total"] is None and out[1]["sized_at"] is None


# ── корзина ──

def _trash(tmp_path, folders, **kw):
    return D.Trash(tmp_path / "trash.json",
                   allowed_fn=lambda: {os.path.normcase(f["path"]) for f in folders},
                   index_fn=lambda: folders, fresh_sec=0, **kw)


def test_trash_move_restore_purge(tmp_path):
    d = _folder(tmp_path, "46161_data")
    folders = [_index_rec(d)]
    t = _trash(tmp_path, folders)
    rec, err = t.move(str(d), order_ref="К46161", by="тест")
    assert err is None
    assert D.TRASH_NAME in rec["dst"] and os.path.isdir(rec["dst"]) and not os.path.isdir(str(d))
    assert rec["state"] == "trashed" and rec["order_ref"] == "К46161"
    assert t.list("trashed")[0]["id"] == rec["id"]

    back, err = t.restore(rec["id"])
    assert err is None and back["state"] == "restored" and os.path.isdir(str(d))

    rec2, _ = t.move(str(d), order_ref="К46161", by="тест")
    gone, err = t.purge(rec2["id"], by="тест")
    assert err is None and gone["state"] == "purged" and not os.path.exists(rec2["dst"])


def test_trash_keeps_folder_inside_same_volume(tmp_path):
    """Корзина — переименование на том же томе: терабайты не копируются и возврат мгновенный."""
    d = _folder(tmp_path, "46162")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, err = t.move(str(d), by="тест")
    assert err is None
    assert os.path.splitdrive(rec["dst"])[0].upper() == os.path.splitdrive(str(d))[0].upper()


def test_delete_now_removes_without_trash(tmp_path):
    d = _folder(tmp_path, "46163")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, err = t.delete_now(str(d), order_ref="К46163", by="тест")
    assert err is None and rec["state"] == "purged" and rec["dst"] is None
    assert not os.path.exists(str(d))


def test_refuses_folder_outside_index(tmp_path):
    d = _folder(tmp_path, "46164")
    t = _trash(tmp_path, [])                       # индекс пуст — папки в нём нет
    rec, err = t.move(str(d))
    assert rec is None and "нет в индексе" in err
    assert os.path.isdir(str(d))


def test_refuses_volume_root(tmp_path):
    t = _trash(tmp_path, [])
    drive = os.path.splitdrive(str(tmp_path))[0] or "C:"
    _, err = t.move(drive + os.sep)
    assert err and "корень тома" in err


def test_refuses_system_drive_even_if_indexed(tmp_path, monkeypatch):
    """Системный том не трогаем, даже если папка в индексе и все прочие проверки прошли бы."""
    d = _folder(tmp_path, "46172")
    monkeypatch.setenv("SystemDrive", os.path.splitdrive(str(d))[0] or "C:")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, err = t.move(str(d))
    assert rec is None and "системном томе" in err
    assert os.path.isdir(str(d))


def test_refuses_folder_in_live_job(tmp_path):
    """Папка задания станции в работе — не трогаем: исполнитель её держит и пишет в неё."""
    d = _folder(tmp_path, "46165")
    folders = [_index_rec(d)]
    t = _trash(tmp_path, folders, busy_fn=lambda: {str(d)})
    rec, err = t.move(str(d))
    assert rec is None and "идёт работа станции" in err
    assert os.path.isdir(str(d))


def test_refuses_folder_being_written(tmp_path):
    """Правило выгрузки: в папке меняли файлы только что — в неё, похоже, ещё пишут."""
    d = _folder(tmp_path, "46166")
    folders = [_index_rec(d, newest=D._iso(__import__("time").time()))]
    t = D.Trash(tmp_path / "trash.json", allowed_fn=lambda: {os.path.normcase(str(d))},
                index_fn=lambda: folders, fresh_sec=600)
    rec, err = t.move(str(d))
    assert rec is None and "ещё пишут" in err


def test_refuses_second_pass_over_trashed_folder(tmp_path):
    d = _folder(tmp_path, "46167")
    folders = [_index_rec(d)]
    t = _trash(tmp_path, folders)
    rec, _ = t.move(str(d))
    # запись в корзине индекс когда-нибудь увидит как папку — и переносить её второй раз нельзя
    folders.append(_index_rec(rec["dst"]))
    again, err = t.move(rec["dst"])
    assert again is None and "уже в корзине" in err


def test_restore_refuses_when_source_taken(tmp_path):
    d = _folder(tmp_path, "46168")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, _ = t.move(str(d))
    _folder(tmp_path, "46168")                     # на прежнее место легло что-то другое
    back, err = t.restore(rec["id"])
    assert back is None and "уже что-то лежит" in err


def test_purge_twice_is_not_second_delete(tmp_path):
    d = _folder(tmp_path, "46169")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, _ = t.move(str(d))
    first, err1 = t.purge(rec["id"], by="тест")
    second, err2 = t.purge(rec["id"], by="тест")
    assert err1 is None and err2 is None
    assert first["purged_at"] == second["purged_at"]


def test_state_survives_restart(tmp_path):
    d = _folder(tmp_path, "46170")
    t = _trash(tmp_path, [_index_rec(d)])
    rec, _ = t.move(str(d))
    again = D.Trash(tmp_path / "trash.json", allowed_fn=lambda: set())
    assert [i["id"] for i in again.list()] == [rec["id"]]


@pytest.mark.skipif(os.name != "nt", reason="проверка занятого файла — через CreateFileW")
def test_refuses_when_file_is_held_open(tmp_path):
    """Задача, открытая в PC-3000, держит свои файлы — такую папку не переносим."""
    d = _folder(tmp_path, "46171")
    t = _trash(tmp_path, [_index_rec(d)])
    with open(d / "a.bin", "r+b"):                 # обычный open() Windows держит без общего доступа на запись
        rec, err = t.move(str(d))
    assert rec is None and "держит открытым" in err
    assert os.path.isdir(str(d))
