"""We.Bit · задачі, повернуті з PAID\\CLOSE у роботу (погоджено з Іваном 14.11.2026, діє з листопада 2026).

Викликається з wspay.py після щоденного знімка (03:40 за Києвом). Порядок (лише робочі дні):
  1. задача вийшла з PAID\\CLOSE не в ⛔️Definitively closed і не в CANCELLED → коментар We.Bit з проханням
     написати причину повернення (згадка автора й виконавця задачі);
  2. через 1 робочий день без відповіді в коментарях → другий коментар: якщо коментаря не буде протягом
     робочого дня, задачу буде переведено в CANCELLED;
  3. ще через 1 робочий день без відповіді → етап CANCELLED і коментар про це.
Відповідь — будь-який коментар не від We.Bit після нашого. Якщо задачу повернули в PAID\\CLOSE, перевели
в ⛔️Definitively closed або CANCELLED — спостереження знімається.

REOPEN_MODE: on (за замовчуванням) — пише в Worksection; dry — лише рахує, нічого не пише.
Репозиторій публічний: у лог — лише кількості.
"""
import datetime as dt
import os

import wsreport as W

INTRO = "Привіт, це We.bit!"
CANCEL_TAG = "CANCELLED"
TEXT1 = (INTRO + "\n\nЗадачу повернули з етапу PAID\\CLOSE у роботу (зараз: {stage}). Задачі в PAID\\CLOSE потрапляють "
         "у розрахунок зарплати, тому напишіть, будь ласка, у коментарі причину повернення.")
TEXT2 = (INTRO + "\n\nПричини повернення задачі з PAID\\CLOSE у коментарях досі немає. Якщо коментаря не буде протягом "
         "наступного робочого дня, задачу буде переведено в CANCELLED.")
TEXT3 = (INTRO + "\n\nПояснення, чому задачу повернули з PAID\\CLOSE, не надійшло — задачу переведено в CANCELLED. "
         "Якщо це помилка, поверніть потрібний етап і напишіть причину в коментарі.")


def workdays_between(a, b, holidays):
    n, d = 0, a + dt.timedelta(days=1)
    while d <= b:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    return n


def tag_list(t):
    tg = (t or {}).get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    return [str(x.get("title") if isinstance(x, dict) else x) for x in (tg or [])]


def people(t):
    who = []
    for k in ("user_from", "user_to"):
        e = (((t or {}).get(k) or {}).get("email") or "").lower()
        if e and e != "noone" and e not in who:
            who.append(e)
    return who


def answered(ws, tid, since_iso):
    """Чи є коментар не від We.Bit після нашого (дата у форматі Worksection)."""
    try:
        data = ws.call("get_comments", id_task=tid)
    except W.WSError:
        return False
    since = dt.datetime.fromisoformat(since_iso)
    for c in data.get("data", []) or []:
        txt = str(c.get("text") or "")
        if txt.strip().startswith(INTRO) or "We.bit" in txt[:40]:
            continue
        raw = str(c.get("date_added") or "")
        try:
            when = dt.datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if when > since:
            return True
    return False


def run(ws, cfg, tasks, events, watch, today, holidays, from_email="", mode=None, log=print):
    """tasks — {id: task} з тегами; events — події знімка; watch — стан спостереження (змінюється на місці)."""
    mode = mode or os.environ.get("REOPEN_MODE") or "on"
    pay_names = W.pay_tags(cfg)
    workday = today.weekday() < 5 and today.isoformat() not in holidays
    now = dt.datetime.now().replace(microsecond=0)
    stats = {"reopen_new": 0, "reopen_c1": 0, "reopen_c2": 0, "reopen_cancel": 0, "reopen_resolved": 0, "reopen_errors": 0}

    for e in events:
        if e["event"] == "reopened" and e["task"] not in watch:
            watch[e["task"]] = {"since": today.isoformat(), "step": 0, "stage": e.get("stage") or ""}
            stats["reopen_new"] += 1

    def post(tid, text, who):
        if mode == "dry":
            return True
        params = {"id_task": tid, "text": text}
        if who:
            params["mention"] = ",".join(who)
        if from_email:
            params["email_user_from"] = from_email
        try:
            ws.call("post_comment", **params)
            return True
        except W.WSError as ex:
            stats["reopen_errors"] += 1
            log("  помилка post_comment: %s" % str(ex)[:160])
            return False

    for tid in list(watch):
        w = watch[tid]
        t = tasks.get(tid)
        stage = W.stage_from_tags(tag_list(t), pay_names) if t else ""
        su = stage.upper()
        if not t or su.startswith("PAID") or "DEFINITIVELY" in su or su.startswith("CANCEL"):
            watch.pop(tid)                              # повернули в оплату, закрили остаточно, скасували або видалили
            stats["reopen_resolved"] += 1
            continue
        if w["step"] >= 1 and answered(ws, tid, w["last_at"]):
            watch.pop(tid)
            stats["reopen_resolved"] += 1
            continue
        if not workday:
            continue
        last = dt.date.fromisoformat(w.get("last") or w["since"])
        who = people(t)
        if w["step"] == 0:
            if post(tid, TEXT1.format(stage=stage or "без етапу"), who):
                w.update(step=1, last=today.isoformat(), last_at=now.isoformat(), stage=stage)
                stats["reopen_c1"] += 1
        elif w["step"] == 1 and workdays_between(last, today, holidays) >= 1:
            if post(tid, TEXT2, who):
                w.update(step=2, last=today.isoformat(), last_at=now.isoformat())
                stats["reopen_c2"] += 1
        elif w["step"] == 2 and workdays_between(last, today, holidays) >= 1:
            ok = mode == "dry"
            if not ok:
                minus = [x for x in tag_list(t) if x not in pay_names and x.strip() not in W.TEAM_TAGS]
                try:
                    params = {"id_task": tid, "plus": CANCEL_TAG}
                    if minus:
                        params["minus"] = ",".join(minus)
                    ws.call("update_task_tags", **params)
                    ok = True
                except W.WSError as ex:
                    stats["reopen_errors"] += 1
                    log("  не вдалося змінити етап на CANCELLED: %s" % str(ex)[:160])
            if ok and post(tid, TEXT3, who):
                w.update(step=3, last=today.isoformat(), last_at=now.isoformat(), cancelled=True)
                stats["reopen_cancel"] += 1
                watch.pop(tid)
    return stats
