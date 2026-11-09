"""Повторяющиеся напоминания о порядке в задачах (вызывается из wsremind.py, тот же запуск в 9:00 по будням).

Правила (каждое — комментарий в задаче с упоминанием нужных людей):
  fill    — открытая задача без типа оплаты / без этапа, или по ней уже есть часы,
            а этап PLANNED или пустой → автору задачи (если автора нет — исполнителю).
  overdue — открытая задача, срок прошёл, а этап ещё до сдачи → исполнителю и руководителю направления.
  pause   — задача на PAUSE больше 15 дней → автору задачи (если автора нет — исполнителю).
  done    — задача в DONE больше 15 дней без PAID\\CLOSE → менеджеру проекта (ответственный проекта в Worksection).

Записи времени короче 10 минут — системные: в правилах не учитываются.
Правила действуют только для задач, созданных с NUDGE_SINCE (по умолчанию 2026-11-01); старые разбираются вручную.
Пока проблема не исправлена и ничего не менялось — напоминание повторяется каждые 3 рабочих дня.
Когда проблема исправлена — один раз пишем «Дякую за роботу!» тем же людям.
Если что-то по задаче поменялось, а проблема осталась (например, перенесли срок, но он снова прошёл), —
отсчёт 3 рабочих дней начинается заново. Несколько правил по одной задаче — один общий комментарий в день.
"""
import datetime as dt
import os

import wsreport as W

REPEAT_WORKDAYS = 3
PAUSE_DAYS = 15
DONE_DAYS = 15
DELIVERED = ("DONE", "TESTING BY CLIENT", "TESTING BY TEAM", "PAID", "CANCEL", "CLOSE", "DEFINITIVELY")
INTRO = "Привіт, це We.bit!"
THANKS = "We.bit задоволений: все виправлено. Дякую за роботу!"
MIN_ENTRY_MINUTES = 10


def _tags(t):
    tg = t.get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    if isinstance(tg, list):
        return [str(x.get("title") if isinstance(x, dict) else x) for x in tg]
    return []


def _workdays_between(a, b, holidays):
    """Рабочих дней после a и до b включительно."""
    n, d = 0, a + dt.timedelta(days=1)
    while d <= b:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    return n


def _norm_name(s):
    return " ".join(sorted(str(s or "").lower().replace("ʼ", "'").split()))


def run(ws, cfg, tasks, team, st, today, mode, holidays, test_email="", from_email="", rules=None, log=print):
    """team — {email: user} сотрудников отчёта IT-команд. Возвращает статистику (только количества)."""
    rules = set(rules or ("fill", "overdue", "pause", "done"))
    since_new = dt.date.fromisoformat(os.environ.get("NUDGE_SINCE") or "2026-11-01")
    pay_names = W.pay_tags(cfg)
    pay_set = set(pay_names)
    nst = st.setdefault("nudges", {})
    nstage = st.setdefault("nstage", {})
    stats = {r + "_active": 0 for r in ("fill", "overdue", "pause", "done")}
    stats.update({"nudge_comments": 0, "nudge_thanks": 0, "nudge_errors": 0})

    # все пользователи (руководители и менеджеры могут быть вне отчёта IT-команд)
    all_users = ws.call("get_users").get("data", [])
    by_name = {_norm_name(u.get("name")): (u.get("email") or "").lower() for u in all_users if u.get("email")}
    name_of = {(u.get("email") or "").lower(): (u.get("name") or "").strip() for u in all_users}
    # руководитель направления по отделу (из планов в config.json)
    head_of = {}
    for p in cfg.get("plans") or []:
        he = by_name.get(_norm_name(p.get("head")))
        for d in p.get("departments") or []:
            if he:
                head_of[d.lower()] = he
    # менеджер проекта = ответственный проекта в Worksection
    proj_mgr = {}
    try:
        for pr in ws.call("get_projects").get("data", []) or []:
            e = ((pr.get("user_to") or {}).get("email") or "").lower()
            if e and e != "noone":
                proj_mgr[str(pr.get("id"))] = e
                proj_mgr[pr.get("name") or ""] = e
    except W.WSError:
        pass
    # часы по задачам и дата последней записи — за 120 дней
    hours, last_work = {}, {}
    for c in W.load_costs(ws, today - dt.timedelta(days=120), today):
        tid = str((c.get("task") or {}).get("id") or "")
        if not tid:
            continue
        h = W.parse_hours(c.get("time"))
        if h * 60 < MIN_ENTRY_MINUTES - 1e-6:     # записи короче 10 минут — системные, не учитываем
            continue
        hours[tid] = hours.get(tid, 0.0) + h
        d = W.parse_date(c.get("date"))
        if d and (tid not in last_work or d > last_work[tid]):
            last_work[tid] = d

    active = {}          # key -> {tid, rule, sig, who:[emails], text}
    tasks_by_id = {}
    for t in tasks:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        tasks_by_id[tid] = t
        created = W.parse_date(t.get("date_added"))
        if not created or created < since_new:   # только задачи, созданные с NUDGE_SINCE
            continue
        tags = _tags(t)
        stage = W.stage_from_tags(tags, pay_names)
        su = stage.upper()
        is_open = str(t.get("status", "")).lower() != "done"
        email = ((t.get("user_to") or {}).get("email") or "").lower()
        in_team = email in team
        rec = nstage.get(tid) or {}
        # с какого дня задача на текущем этапе: при первом знакомстве — по закрытию / последней работе / созданию
        if rec.get("stage") != stage:
            if "stage" in rec:
                since = today
            else:
                since = (W.parse_date(t.get("date_closed")) if su.startswith("DONE") else None) \
                    or last_work.get(tid) or W.parse_date(t.get("date_added")) or today
            rec = {"stage": stage, "since": since.isoformat()}
            nstage[tid] = rec
        stage_days = (today - dt.date.fromisoformat(rec["since"])).days
        delivered = any(su.startswith(x) or x in su for x in DELIVERED)

        if "fill" in rules and is_open and in_team:
            miss = []
            if not any(x in pay_set for x in tags):
                miss.append("тип оплати")
            if not stage:
                miss.append("етап")
            worked = hours.get(tid, 0) > 0 and (not stage or su.startswith("PLANNED"))
            if miss or worked:
                parts = []
                if miss:
                    parts.append("У задачі бракує: %s. Без цього я плутаюсь у звітах, а я ж робот — мені не можна плутатись." % ", ".join(miss))
                if worked:
                    parts.append("По задачі вже є години, а етап ще %s — переведи, будь ласка, в IN PROGRESS, щоб я не думав, що вона ще спить."
                                 % (stage or "не проставлений"))
                author = ((t.get("user_from") or {}).get("email") or "").lower()
                if not author or author == "noone":
                    author = email                # автора нет — пишем исполнителю
                active["fill:" + tid] = {"tid": tid, "rule": "fill", "who": [author],
                                         "sig": "|".join(miss) + ("|worked" if worked else ""),
                                         "text": " ".join(parts) + " Заповни, будь ласка"}
        due = W.parse_date(t.get("date_end"))
        if "overdue" in rules and is_open and in_team and due and due < today and not delivered:
            who = [email]
            head = head_of.get(str((team.get(email) or {}).get("department") or "").lower())
            if head and head != email:
                who.append(head)
            active["overdue:" + tid] = {"tid": tid, "rule": "overdue", "who": who, "sig": due.isoformat() + "|" + stage,
                                        "text": "Строк задачі минув (%s), а задача ще тут. Онови, будь ласка, строк або етап — я переживаю (наскільки роботи вміють)."
                                        % due.strftime("%d.%m")}
        if "pause" in rules and is_open and in_team and su.startswith("PAUSE") and stage_days > PAUSE_DAYS:
            author = ((t.get("user_from") or {}).get("email") or "").lower()
            if not author or author == "noone":
                author = email                    # автора нет — пишем исполнителю
            active["pause:" + tid] = {"tid": tid, "rule": "pause", "who": [author], "sig": stage,
                                      "text": "Ця задача на паузі вже більше %d днів. Здається, вона заснула. "
                                              "Розбудимо (IN PROGRESS) чи відпустимо (CANCELLED)?" % PAUSE_DAYS}
        if "done" in rules and in_team and su.startswith("DONE") and stage_days > DONE_DAYS:
            pr = t.get("project") or {}
            mgr = proj_mgr.get(str(pr.get("id"))) or proj_mgr.get(pr.get("name") or "")
            if mgr:
                active["done:" + tid] = {"tid": tid, "rule": "done", "who": [mgr], "sig": stage,
                                         "text": "%s, задача зроблена, а гроші ще ні. Більше %d днів без PAID\\CLOSE. "
                                                 "Виставте, будь ласка, акт клієнту або напишіть у відповідь на цей коментар, "
                                                 "що заважає її закрити."
                                                 % (name_of.get(mgr) or mgr, DONE_DAYS)}

    for k, v in active.items():
        stats[v["rule"] + "_active"] += 1

    workday = today.weekday() < 5 and today.isoformat() not in holidays
    outbox = {}          # tid -> {"texts": [], "who": set(), "keys": []}

    def queue(tid, text, who, key, kind):
        if mode == "test" and test_email.lower() not in who:
            return
        o = outbox.setdefault(tid, {"texts": [], "who": [], "keys": []})
        o["texts"].append(text)
        o["who"] += [w for w in who if w and w not in o["who"]]
        o["keys"].append((key, kind))

    # новые и повторные напоминания
    for key, v in active.items():
        rec = nst.get(key)
        if rec is None:
            if workday:
                queue(v["tid"], v["text"], v["who"], key, "new")
            continue
        if rec.get("sig") != v["sig"]:           # что-то поменялось, а проблема осталась — отсчёт заново
            nst[key] = {**rec, "sig": v["sig"], "last": today.isoformat()}
            continue
        last = dt.date.fromisoformat(rec.get("last") or today.isoformat())
        if workday and _workdays_between(last, today, holidays) >= REPEAT_WORKDAYS:
            queue(v["tid"], v["text"] + " (нагадування №%d)" % ((rec.get("count") or 1) + 1), v["who"], key, "repeat")
    # проблемы, которые исправили
    for key in list(nst):
        if key in active:
            continue
        rec = nst[key]
        rule, tid = key.split(":", 1)
        if rule not in rules:
            continue
        if rec.get("sent") and workday:
            queue(tid, THANKS, rec.get("who") or [], key, "thanks")
        else:
            nst.pop(key, None)

    for tid, o in outbox.items():
        only_thanks = all(k == "thanks" for _, k in o["keys"])
        body = [x for x, (_, k) in zip(o["texts"], o["keys"]) if k != "thanks"]
        if only_thanks:
            text = THANKS
        else:
            text = INTRO + "\n\n" + ("Дякую за роботу!\n\n" if any(k == "thanks" for _, k in o["keys"]) else "") + "\n\n".join(body)
        ok = True
        if mode != "dry":
            params = {"id_task": tid, "text": text, "mention": ",".join(o["who"])}
            if from_email:
                params["email_user_from"] = from_email
            try:
                ws.call("post_comment", **params)
            except W.WSError as e:
                ok = False
                stats["nudge_errors"] += 1
                log("  ошибка post_comment: %s" % str(e)[:200])
        if not ok:
            continue
        if mode != "dry":
            stats["nudge_comments"] += 1
        for key, kind in o["keys"]:
            if kind == "thanks":
                stats["nudge_thanks"] += 1
                if mode != "dry":
                    nst.pop(key, None)
            elif mode != "dry":
                v = active[key]
                nst[key] = {"sig": v["sig"], "last": today.isoformat(), "who": v["who"], "sent": True,
                            "count": (nst.get(key) or {}).get("count", 0) + 1}
    stats["nudge_tasks_to_comment"] = len(outbox)
    # чистим записи о задачах, которых больше нет
    for key in list(nst):
        if key.split(":", 1)[1] not in tasks_by_id:
            nst.pop(key, None)
    for tid in list(nstage):
        if tid not in tasks_by_id:
            nstage.pop(tid, None)
    return stats
