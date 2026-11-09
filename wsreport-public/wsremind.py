#!/usr/bin/env python3
"""Напоминания исполнителям о задачах, у которых скоро срок (отдельно от wsreport.py, отчёты не трогает).

Каждый будний день находит открытые задачи Worksection, у которых:
  • до срока (date_end) ровно REMIND_DAYS рабочих дней (по умолчанию 4): субботы, воскресенья и праздники
    из config.json (holidays) не считаются; запуск только по будням в 9:00 по Киеву;
  • исполнитель — сотрудник из отчёта IT-команд (те же фильтры, что в wsreport.py);
  • этап ещё «до сдачи» (не DONE, не TESTING BY CLIENT/TEAM, не PAID\\CLOSE, не CANCELLED и т.п.)
    и не менялся последние STAGE_STILL_DAYS дней (этапы задач запоминаются между запусками);
  • короткие задачи (от создания до срока меньше REMIND_DAYS+1 рабочих дней) тоже получают напоминание:
    за (рабочих дней от создания до срока − 1) дней, т.е. в середине срока; если на задачу дали 1 рабочий день —
    утром в день срока. Пример: создана 01.10 (ср), срок 03.10 (пт) → напоминание 02.10 (чт);
и пишет в задачу комментарий с упоминанием исполнителя:
  «Привіт, це We.bit! Мої сенсори показують, що дедлайн цієї задачі вже на горизонті (ДД.ММ). Все під контролем чи кличемо підкріплення?»
По каждой задаче напоминание уходит один раз на каждый срок: если срок перенесли — придёт снова к новой дате.

Режимы (переменная REMIND_MODE или флаг --mode):
  dry  — ничего не пишет в Worksection, только считает (по умолчанию при локальном запуске);
  test — пишет только в задачи исполнителя REMIND_TEST_EMAIL (проверить на себе);
  send — пишет всем (по умолчанию в GitHub Actions).
Состояние (этапы и отправленные напоминания) — output/remind/state.json; в GitHub оно хранится зашифрованным.
В лог выводятся только количества: репозиторий публичный. Подробный список — флаг --list (для локального запуска).

Запуск:  python wsremind.py                     (ключ — WS_API_KEY или config.json, как у wsreport.py)
         python wsremind.py --mode test --test-email me@wecode.ua
         python wsremind.py --date 2026-11-03 --list
         python wsremind.py --task 1692562        (проверка: комментарий только в одну задачу, без фильтров)
         python wsremind.py --cleanup 1692562     (удалить из задачи наши напоминания, например тестовые)
"""
import argparse
import datetime as dt
import json
import os
import sys

import wsnudge
import wsreport as W

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "output", "remind", "state.json")

TEXT = ("Привіт, це We.bit! Мої сенсори показують, що дедлайн цієї задачі вже на горизонті ({date}). "
        "Все під контролем чи кличемо підкріплення?")

# Этапы, на которых работа уже сдана или задача закрыта — им не напоминаем.
DELIVERED = ("DONE", "TESTING BY CLIENT", "TESTING BY TEAM", "PAID", "CANCEL", "CLOSE", "DEFINITIVELY")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"tasks": {}, "runs": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def workdays_left(today, due, holidays):
    """Сколько рабочих дней от завтра до срока включительно (выходные и праздники не считаются)."""
    n, d = 0, today + dt.timedelta(days=1)
    while d <= due:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    return n


def cleanup(ws, tid):
    """Удалить из задачи комментарии-напоминания (по тексту). Метод удаления в документации API не описан,
    поэтому пробуем известные варианты и печатаем, что ответил сервер."""
    data = ws.call("get_comments", id_task=tid)
    mine = [c for c in data.get("data", []) if "скоро кінцевий строк задачі" in str(c.get("text", ""))]
    print("Задача #%s: напоминаний найдено %d" % (tid, len(mine)))
    for c in mine:
        cid = c.get("id")
        for params in ({"id_comment": cid}, {"id_task": tid, "id_comment": cid}):
            try:
                ws.call("delete_comment", **params)
                print("  комментарий %s удалён (delete_comment %s)" % (cid, ",".join(params)))
                break
            except W.WSError as e:
                print("  delete_comment %s: %s" % (",".join(params), str(e)[:160]))
        else:
            print("  комментарий %s удалить через API не удалось — удалите вручную в задаче" % cid)


def tags_of(t):
    tg = t.get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    if isinstance(tg, list):
        return [str(x.get("title") if isinstance(x, dict) else x) for x in tg]
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default=os.environ.get("REMIND_MODE") or "dry", choices=("dry", "test", "send"))
    ap.add_argument("--test-email", default=os.environ.get("REMIND_TEST_EMAIL", ""))
    ap.add_argument("--from-email", default=os.environ.get("REMIND_FROM", ""),
                    help="от чьего имени комментарий (email пользователя Worksection); по умолчанию — владелец ключа")
    ap.add_argument("--days", type=int, default=env_int("REMIND_DAYS", 4))
    ap.add_argument("--still", type=int, default=env_int("STAGE_STILL_DAYS", 2),
                    help="сколько дней этап не должен меняться")
    ap.add_argument("--date", default=os.environ.get("REMIND_DATE") or None,
                    help="сегодняшняя дата YYYY-MM-DD (для проверки)")
    ap.add_argument("--task", default=os.environ.get("REMIND_TASK", ""),
                    help="проверка: написать комментарий только в эту задачу (номер), без фильтров")
    ap.add_argument("--cleanup", default=os.environ.get("REMIND_CLEANUP", ""),
                    help="удалить из задачи (номер) комментарии-напоминания этого скрипта")
    ap.add_argument("--list", action="store_true", help="вывести список задач (не использовать в публичных логах)")
    a = ap.parse_args()

    cfg = W.load_config()
    if not cfg["api_key"]:
        sys.exit("Нет API-ключа: WS_API_KEY или config.json")
    if a.mode == "test" and not a.test_email:
        sys.exit("Режим test: укажите REMIND_TEST_EMAIL")
    today = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    cfg["_month"] = today.isoformat()[:7]

    ws = W.Worksection(cfg["account"], cfg["api_key"])
    if a.cleanup:
        cleanup(ws, a.cleanup.strip().lstrip("#"))
        return
    users = W.load_users(ws, cfg)                  # сотрудники отчёта IT-команд
    emails = {(u.get("email") or "").lower(): u for u in users.values()}
    tasks = W.load_tasks(ws)
    pay_names = W.pay_tags(cfg)
    holidays = set(cfg.get("holidays") or [])

    if a.task:                                  # проверка на одной задаче
        tid = a.task.strip().lstrip("#")
        t = next((x for x in tasks if str(x.get("id")) == tid), None)
        if not t:
            sys.exit("Задача #%s не найдена среди задач Worksection" % tid)
        email = ((t.get("user_to") or {}).get("email") or "").lower()
        due = W.parse_date(t.get("date_end"))
        text = TEXT.format(date=due.strftime("%d.%m") if due else "скоро")
        params = {"id_task": t.get("id"), "text": text}
        if email and email != "noone":
            params["mention"] = email
        if a.from_email:
            params["email_user_from"] = a.from_email
        ws.call("post_comment", **params)
        print("Проверка: комментарий записан в задачу #%s (упоминание исполнителя: %s, срок: %s)" % (
            tid, "да" if "mention" in params else "нет исполнителя", "есть" if due else "не задан"))
        return

    st = load_state()
    seen = st["tasks"]
    stats = {"open_with_deadline": 0, "due_soon": 0, "delivered_stage": 0, "stage_moved": 0,
             "already_reminded": 0, "not_team": 0, "to_remind": 0, "sent": 0, "errors": 0}
    picked = []
    alive = set()
    for t in tasks:
        tid = str(t.get("id") or "")
        if not tid or str(t.get("status", "")).lower() == "done":
            continue
        alive.add(tid)
        stage = W.stage_from_tags(tags_of(t), pay_names)
        rec = seen.get(tid) or {}
        if "stage" not in rec:                  # впервые видим задачу — движения этапа пока не знаем
            rec = {**rec, "stage": stage, "since": today.isoformat(), "moved": False}
        elif rec["stage"] != stage:             # этап сменился — запоминаем день смены
            rec = {**rec, "stage": stage, "since": today.isoformat(), "moved": True}
        seen[tid] = rec

        due = W.parse_date(t.get("date_end"))
        if not due:
            continue
        stats["open_with_deadline"] += 1
        left = (due - today).days
        if today.weekday() >= 5 or today.isoformat() in holidays:   # в выходные и праздники не пишем
            continue
        if due < today:                          # срок уже прошёл — это просрочка, не напоминание
            continue
        added = W.parse_date(t.get("date_added"))
        if added and added >= today:            # только что созданные задачи не трогаем
            continue
        target = a.days                          # обычно — ровно за REMIND_DAYS рабочих дней
        if added:
            span = workdays_left(added, due, holidays)   # сколько рабочих дней дали на задачу
            if span <= a.days:                   # короткая задача: напоминаем в середине срока
                target = max(0, span - 1)
        if workdays_left(today, due, holidays) != target:
            continue
        stats["due_soon"] += 1
        u = t.get("user_to") or {}
        email = (u.get("email") or "").lower()
        if email not in emails:
            stats["not_team"] += 1
            continue
        if any(stage.upper().startswith(x) or x in stage.upper() for x in DELIVERED):
            stats["delivered_stage"] += 1
            continue
        since = dt.date.fromisoformat(rec["since"])
        if rec.get("moved") and (today - since).days < a.still:
            stats["stage_moved"] += 1
            continue
        if rec.get("reminded") == due.isoformat():
            stats["already_reminded"] += 1
            continue
        if a.mode == "test" and email != a.test_email.lower():
            continue
        stats["to_remind"] += 1
        picked.append((t, email, due, stage, left))

    for t, email, due, stage, left in picked:
        text = TEXT.format(date=due.strftime("%d.%m"))
        if a.list:
            print("  %s · %s · срок %s (через %d дн.) · %s · %s" % (
                t.get("id"), email, due.isoformat(), left, stage or "без этапа", (t.get("name") or "")[:70]))
        if a.mode == "dry":
            continue
        params = {"id_task": t.get("id"), "text": text, "mention": email}
        if a.from_email:
            params["email_user_from"] = a.from_email
        try:
            ws.call("post_comment", **params)
            seen[str(t.get("id"))]["reminded"] = due.isoformat()
            stats["sent"] += 1
        except W.WSError as e:
            stats["errors"] += 1
            print("  ошибка post_comment: %s" % str(e)[:200])

    # повторяющиеся напоминания о порядке в задачах (свой режим NUDGE_MODE; в GitHub Actions — send)
    nmode = os.environ.get("NUDGE_MODE") or "dry"
    if a.mode == "dry":
        nmode = "dry"
    rules = [x.strip() for x in (os.environ.get("NUDGE_RULES") or "fill,overdue,pause,done").split(",") if x.strip()]
    nstats = wsnudge.run(ws, cfg, tasks, emails, st, today, nmode, holidays,
                         test_email=a.test_email, from_email=a.from_email, rules=rules)
    stats.update(nstats)

    # чистим закрытые задачи, пишем журнал запусков (только количества)
    st["tasks"] = {k: v for k, v in seen.items() if k in alive}
    st["runs"] = (st["runs"] + [{"date": today.isoformat(), "mode": a.mode, **stats}])[-120:]
    save_state(st)

    print("Режим: %s · за %d раб. дн. до срока · этап без изменений: от %d дн." % (a.mode, a.days, a.still))
    for k, label in (("open_with_deadline", "открытых задач со сроком"), ("due_soon", "срок через %d раб. дн." % a.days),
                     ("not_team", "  из них исполнитель не из IT-команд"), ("delivered_stage", "  уже сданы (DONE/TESTING и т.п.)"),
                     ("stage_moved", "  этап недавно менялся"), ("already_reminded", "  напоминание уже было"),
                     ("to_remind", "к напоминанию"), ("sent", "отправлено"), ("errors", "ошибок")):
        print("  %-40s %d" % (label, stats[k]))
    print("Порядок в задачах (режим %s, правила %s):" % (nmode, ",".join(rules)))
    for k, label in (("fill_active", "не заполнены тип оплаты/этап"), ("overdue_active", "просрочены"),
                     ("pause_active", "на PAUSE больше 15 дней"), ("done_active", "в DONE больше 15 дней без оплаты"),
                     ("nudge_tasks_to_comment", "задач с комментарием сегодня"), ("nudge_comments", "отправлено комментариев"),
                     ("nudge_thanks", "из них «Дякую за роботу!»"), ("nudge_errors", "ошибок")):
        print("  %-40s %d" % (label, stats.get(k, 0)))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Напоминания о сроках (%s, режим %s)\n\n" % (today.isoformat(), a.mode))
            f.write("| Показатель | Кол-во |\n|---|---|\n")
            for k in ("due_soon", "to_remind", "sent", "errors", "fill_active", "overdue_active", "pause_active",
                      "done_active", "nudge_tasks_to_comment", "nudge_comments", "nudge_thanks", "nudge_errors"):
                f.write("| %s | %d |\n" % (k, stats.get(k, 0)))
    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
