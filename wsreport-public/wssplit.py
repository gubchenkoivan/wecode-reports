"""Разбить сводную задачу на подзадачи по записям трудозатрат.

Берёт все записи времени задачи (только самой задачи, не подзадач) и группирует их:
  person — одна подзадача на сотрудника;
  month  — одна подзадача на сотрудника и месяц (по умолчанию);
  entry  — одна подзадача на каждую запись.
Подзадача: исполнитель — автор записей, начало и срок — первая и последняя дата записей группы,
оценка — сумма часов группы, теги — как у родительской задачи.
С --move записи переносятся в подзадачу: add_costs в подзадачу (тот же сотрудник, дата, часы, комментарий),
затем delete_costs в родителе. Если добавить не удалось — исходная запись не удаляется.
Записи без описания работ не переносятся: специалисту один раз пишется комментарий в сводной задаче
с просьбой заполнить описание (нужно для сдачи клиенту); после заполнения следующий запуск их перенесёт.

Режимы: dry — ничего не меняет, только считает; send — создаёт подзадачи (и переносит записи).
В лог — только количества (репозиторий публичный). Подробный план и журнал — в split/ (шифруется в split.enc).
Записи короче 10 минут считаются системными: их не переносим и описание по ним не просим.
Повторный запуск по той же задаче не создаёт дублей: обработанные записи помнятся в журнале.
Закрытую в Worksection задачу разбить нельзя: скрипт ничего не меняет и завершается с ошибкой «Задача закрыта».
"""
import argparse
import datetime as dt
import json
import os
import sys
from collections import OrderedDict

import wsreport as W

MIN_ENTRY_MINUTES = 10   # записи короче 10 минут — системные: не переносим и не просим описание

MONTHS_UK = ["", "січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень",
             "вересень", "жовтень", "листопад", "грудень"]


def _tags(t):
    tg = t.get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    if isinstance(tg, list):
        return [str(x.get("title") if isinstance(x, dict) else x) for x in tg]
    return []


def _hours_str(h):
    m = int(round(h * 60))
    return "%d:%02d" % (m // 60, m % 60)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", default=os.environ.get("SPLIT_TASK", ""))
    ap.add_argument("--group", default=os.environ.get("SPLIT_GROUP") or "month", choices=("person", "month", "entry"))
    ap.add_argument("--move", default=os.environ.get("SPLIT_MOVE") or "yes", choices=("yes", "no"))
    ap.add_argument("--mode", default=os.environ.get("SPLIT_MODE") or "dry", choices=("dry", "send"))
    ap.add_argument("--from-date", default=os.environ.get("SPLIT_FROM") or "", help="только записи с даты YYYY-MM-DD")
    ap.add_argument("--to-date", default=os.environ.get("SPLIT_TO") or "", help="только записи по дату YYYY-MM-DD")
    a = ap.parse_args()
    tid = a.task.strip().lstrip("#")
    if not tid.isdigit():
        sys.exit("Укажите номер задачи")

    cfg = W.load_config()
    ws = W.Worksection(cfg["account"], cfg["api_key"])
    os.makedirs("split", exist_ok=True)
    jpath = os.path.join("split", "journal.json")
    journal = json.load(open(jpath, encoding="utf-8")) if os.path.exists(jpath) else {}
    jt = journal.setdefault(tid, {"groups": {}, "moved": {}})
    jt.setdefault("asked", {})

    task = ws.call("get_task", id_task=tid).get("data") or {}
    if not task:
        sys.exit("Задача не найдена")
    if task.get("parent"):
        print("Внимание: задача сама является подзадачей")
    if str(task.get("status") or "").lower() == "done":
        print("Задача закрыта — разбить её нельзя")
        sys.exit(1)
    proj = (task.get("project") or {}).get("id")
    tags = _tags(task)
    author = ((task.get("user_from") or {}).get("email") or "").lower()

    users = {(u.get("email") or "").lower(): (u.get("name") or "").strip()
             for u in ws.call("get_users").get("data", []) if u.get("email")}

    costs = ws.call("get_costs", id_task=tid).get("data", []) or []
    own = [c for c in costs if str((c.get("task") or {}).get("id") or "") == tid]
    d_from = dt.date.fromisoformat(a.from_date) if a.from_date else None
    d_to = dt.date.fromisoformat(a.to_date) if a.to_date else None

    groups = OrderedDict()
    empty = OrderedDict()
    skipped_done = skipped_period = no_user = with_money = timer = short = 0
    for c in sorted(own, key=lambda c: (W.parse_date(c.get("date")) or dt.date.min, str(c.get("id")))):
        cid = str(c.get("id"))
        if cid in jt["moved"] or any(cid in g.get("costs", []) for g in jt["groups"].values()):
            skipped_done += 1
            continue
        d = W.parse_date(c.get("date"))
        if (d_from and d and d < d_from) or (d_to and d and d > d_to):
            skipped_period += 1
            continue
        if W.parse_hours(c.get("time")) * 60 < MIN_ENTRY_MINUTES - 1e-6:
            short += 1
            continue
        email = ((c.get("user_from") or {}).get("email") or "").lower()
        if not email:
            no_user += 1
            continue
        if str(c.get("money") or "0").strip() not in ("", "0", "0.00", "0,00"):
            with_money += 1
        if c.get("is_timer") in (True, 1, "1", "true"):
            timer += 1
        if not str(c.get("comment") or "").strip():   # без описания работ — не переносим, просим заполнить
            empty.setdefault(email, []).append(c)
            continue
        if a.group == "person":
            key = email
        elif a.group == "month":
            key = email + "|" + (d.strftime("%Y-%m") if d else "????")
        else:
            key = email + "|" + cid
        g = groups.setdefault(key, {"email": email, "costs": [], "dates": [], "hours": 0.0, "comments": []})
        g["costs"].append(c)
        if d:
            g["dates"].append(d)
        g["hours"] += W.parse_hours(c.get("time"))
        if c.get("comment"):
            g["comments"].append(str(c.get("comment")).strip())

    plan = []
    for key, g in groups.items():
        name = users.get(g["email"]) or g["email"]
        d1, d2 = (min(g["dates"]), max(g["dates"])) if g["dates"] else (None, None)
        if a.group == "month" and d1:
            title = "%s — %s %d" % (name, MONTHS_UK[d1.month], d1.year)
        elif a.group == "entry":
            first = " ".join((g["comments"][0] if g["comments"] else "").split())
            if len(first) > 120:
                first = first[:117].rstrip(" ,.;") + "…"
            title = "%s — %s" % (d1.strftime("%d.%m.%Y") if d1 else "?", first or ("Роботи — " + name))
        else:
            title = name
        lines = ["Записи трудозатрат перенесено зі зведеної задачі #%s." % tid if a.move == "yes"
                 else "Записи трудозатрат у зведеній задачі #%s." % tid,
                 "Період: %s — %s, разом %s год." % (d1.strftime("%d.%m.%Y") if d1 else "?",
                                                    d2.strftime("%d.%m.%Y") if d2 else "?", _hours_str(g["hours"]))]
        if a.group == "entry" and g["comments"]:
            lines += ["", g["comments"][0]]
        elif g["comments"]:
            lines.append("")
            for c in g["costs"]:
                if c.get("comment"):
                    cd = W.parse_date(c.get("date"))
                    lines.append("%s · %s · %s" % (cd.strftime("%d.%m.%Y") if cd else c.get("date"), c.get("time"),
                                                   str(c.get("comment")).strip()))
        plan.append({"key": key, "title": title, "email": g["email"], "name": name,
                     "datestart": W.fmt_ws(d1) if d1 else None, "dateend": W.fmt_ws(d2) if d2 else None,
                     "hours": round(g["hours"], 2), "text": "\n".join(lines),
                     "costs": [{"id": str(c.get("id")), "date": c.get("date"), "time": c.get("time"),
                                "money": c.get("money"), "comment": c.get("comment"), "email": g["email"]}
                               for c in g["costs"]]})

    people = len({p["email"] for p in plan})
    months = sorted({(p["datestart"] or "")[3:] for p in plan if p["datestart"]})
    allh = sum(p["hours"] for p in plan)
    stats = OrderedDict([
        ("записей в задаче", len(own)),
        ("из них уже обработаны раньше", skipped_done),
        ("вне выбранного периода", skipped_period),
        ("без автора", no_user),
        ("короче 10 минут (системные, не трогаем)", short),
        ("к обработке", sum(len(p["costs"]) for p in plan)),
        ("часов к обработке", round(allh, 1)),
        ("сотрудников", people),
        ("месяцев", len(months)),
        ("подзадач будет создано", len(plan)),
        ("записей без описания (не переносим)", sum(len(v) for v in empty.values())),
        ("из них специалистов попросим заполнить", sum(1 for e, v in empty.items()
                                                   if any(str(c.get("id")) not in jt["asked"] for c in v))),
        ("записи с деньгами", with_money),
        ("записи по таймеру", timer),
        ("подзадач у задачи сейчас", len(task.get("child") or [])),
    ])
    print("Задача: этап %s, статус %s, тегов %d" % (W.stage_from_tags(tags, W.pay_tags(cfg)) or "—",
                                                   task.get("status"), len(tags)))
    print("Режим: %s · группировка: %s · перенос записей: %s" % (a.mode, a.group, a.move))
    for k, v in stats.items():
        print("  %-32s %s" % (k, v))
    if months:
        print("  период записей: %s … %s" % (months[0], months[-1]))

    created = moved = errors = 0
    if a.mode == "send":
        for p in plan:
            g = jt["groups"].get(p["key"])
            if g and g.get("id"):
                sub_id = g["id"]
            else:
                params = {"id_project": proj, "id_parent": tid, "title": p["title"], "email_user_to": p["email"],
                          "text": p["text"], "max_time": _hours_str(p["hours"]), "tags": ",".join(tags) or None,
                          "datestart": p["datestart"], "dateend": p["dateend"]}
                if author and author != "noone":
                    params["email_user_from"] = author
                try:
                    r = ws.call("post_task", **params)
                except W.WSError as e:
                    errors += 1
                    print("  ошибка post_task: %s" % str(e)[:200])
                    continue
                sub_id = str((r.get("data") or {}).get("id") or "")
                if not sub_id:
                    errors += 1
                    print("  post_task не вернул id")
                    continue
                created += 1
                jt["groups"][p["key"]] = {"id": sub_id, "title": p["title"], "costs": []}
            g = jt["groups"][p["key"]]
            for c in p["costs"]:
                if a.move != "yes":
                    g["costs"].append(c["id"])
                    continue
                cd = W.parse_date(c["date"])
                add = {"id_task": sub_id, "time": c["time"], "date": W.fmt_ws(cd) if cd else None, "email_user_from": c["email"],
                       "comment": c["comment"] or None}
                if c.get("money") and str(c["money"]).strip() not in ("0", "0.00", "0,00"):
                    add["money"] = c["money"]
                try:
                    r = ws.call("add_costs", **add)
                    new_id = str(r.get("id") or (r.get("data") or {}).get("id") or "")
                except W.WSError as e:
                    errors += 1
                    print("  ошибка add_costs: %s" % str(e)[:200])
                    continue
                try:
                    ws.call("delete_costs", id=c["id"], id_costs=c["id"])
                except W.WSError as e:
                    errors += 1
                    print("  ошибка delete_costs (запись осталась и в родителе): %s" % str(e)[:200])
                    jt["moved"][c["id"]] = {"to": sub_id, "new": new_id, "deleted": False}
                    continue
                jt["moved"][c["id"]] = {"to": sub_id, "new": new_id, "deleted": True}
                g["costs"].append(c["id"])
                moved += 1
            json.dump(journal, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        # записи без описания: один комментарий в сводной задаче с упоминанием специалистов (по каждой записи — один раз)
        ask = OrderedDict((e, [c for c in v if str(c.get("id")) not in jt["asked"]]) for e, v in empty.items())
        ask = OrderedDict((e, v) for e, v in ask.items() if v)
        asked = 0
        if ask:
            parts = ["Привіт, це We.bit! Знайшов записи часу без опису робіт:"]
            for e, v in ask.items():
                if len(ask) > 1:
                    parts.append(users.get(e) or e)
                for c in v:
                    cd = W.parse_date(c.get("date"))
                    parts.append("— %s, %s" % (cd.strftime("%d.%m.%Y") if cd else c.get("date"), c.get("time")))
            parts.append("Клієнт не вміє читати думки (і я теж). Допиши, будь ласка, що робив — це потрібно для здачі задачі клієнту. "
                         "Після цього запис буде перенесено в окрему підзадачу.")
            try:
                ws.call("post_comment", id_task=tid, text="\n".join(parts), mention=",".join(ask.keys()))
                for v in ask.values():
                    for c in v:
                        jt["asked"][str(c.get("id"))] = dt.date.today().isoformat()
                asked = len(ask)
            except W.WSError as e:
                errors += 1
                print("  ошибка post_comment: %s" % str(e)[:200])
        print("  создано подзадач                 %d" % created)
        print("  попросили заполнить описание     %d" % asked)
        print("  перенесено записей               %d" % moved)
        print("  ошибок                           %d" % errors)

    json.dump({"task": tid, "mode": a.mode, "group": a.group, "move": a.move, "stats": stats, "plan": plan},
              open(os.path.join("split", "preview_%s.json" % tid), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(journal, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Разбивка задачи #%s (режим %s, группировка %s)\n\n" % (tid, a.mode, a.group))
            for k, v in stats.items():
                f.write("- %s: %s\n" % (k, v))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
