"""Синхронизация реестра задач КазМунайГаз (Backlog.xlsx заказчика) с Worksection.

Вход: kmg/rows.json — строки листа «ERP», которые прочитала задача Claude из файла заказчика
(колонки A..M как в файле: A — №, B — система, C — дата обращения, D — контакт, E — описание,
F — приоритет, G — ответственный, H — крайний срок, I — статус, J — дата выполнения, M — комментарий;
поле row — номер строки листа). Для строк, по которым задача уже создана (список known из прошлого
результата), достаточно полей A, I и row.

Что делает:
  1. Задачи проекта SUPP_Казмунайгаз: подзадачи с названием «КМГ №N: …» связаны со строкой №N файла.
  2. Новые строки (номера ещё нет в Worksection, статус в файле не «Готово») → подзадача в задаче
     «Поддержка <Месяц> <Год>!» текущего месяца: исполнитель Ростислав Король, «Оплачувана», IN PROGRESS.
     Если задачи месяца нет — ничего не создаём, в итоге ошибка «нет задачи месяца».
  3. Для связанных задач: этап DONE (или задача закрыта), а в файле статус ещё не «Готово» →
     в kmg/result.json пункт для записи в файл: статус «Готово», дата выполнения, комментарий «Необходимо проверить».
Режимы: dry — ничего не создаёт, только считает; send — создаёт подзадачи.
В лог — только количества (репозиторий публичный); подробности — в kmg/result.json (шифруется).
"""
import datetime as dt
import json
import os
import re
import sys

import wsreport as W

PROJECT = "SUPP_Казмунайгаз"
MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
          "Октябрь", "Ноябрь", "Декабрь"]
TITLE_RE = re.compile(r"^\s*КМГ\s*№\s*(\d+)\s*:")
PRIO = {"высокий": 9, "средний": 5, "низкий": 2}
SRC = "https://kmgkz-my.sharepoint.com/:x:/r/personal/duisenbayev_a_osc_kmg_kz/_layouts/15/Doc.aspx?sourcedoc=%7B64769A04-384D-4D65-BEE3-1C8FB109BB87%7D&file=Backlog.xlsx"


def clean(v):
    return str(v or "").replace("\xa0", " ").strip()


def xdate(v):
    """Дата из Excel: число (серийный день) или текст ДД.ММ.ГГГГ."""
    s = clean(v)
    if not s:
        return None
    try:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):  # в Казахстане день идёт первым: 05/11/2026 = 5 ноября
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def is_ready(status):
    return clean(status).lower().startswith("готов")


def norm(s):
    return re.sub(r"[^\wа-яёіїєґ ]", "", clean(s).lower()).split()


def tags_of(t):
    tg = t.get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    if isinstance(tg, list):
        return [str(x.get("title") if isinstance(x, dict) else x) for x in tg]
    return []


def main():
    mode = os.environ.get("KMG_MODE") or "dry"
    today = dt.date.fromisoformat(os.environ["KMG_DATE"]) if os.environ.get("KMG_DATE") else dt.date.today()
    rows = json.load(open(os.path.join("kmg", "rows.json"), encoding="utf-8"))
    cfg = W.load_config()
    ws = W.Worksection(cfg["account"], cfg["api_key"])
    pay_names = W.pay_tags(cfg)

    projects = ws.call("get_projects").get("data", []) or []
    proj = next((p for p in projects if clean(p.get("name")) == PROJECT), None)
    if not proj:
        sys.exit("Проект %s не найден" % PROJECT)
    pid = proj.get("id")
    tasks = ws.call("get_tasks", id_project=pid, extra="subtasks").get("data", []) or []

    # все задачи и подзадачи проекта плоским списком
    flat = []
    def walk(t, parent=None):
        flat.append((t, parent))
        for c in t.get("child") or []:
            walk(c, t)
    for t in tasks:
        walk(t)

    linked = {}
    for t, parent in flat:
        m = TITLE_RE.match(t.get("name") or "")
        if m:
            linked.setdefault(m.group(1), t)

    if mode == "rename":
        # разовое переименование после перенумерации строк в файле:
        # вход — [{"id": "<id задачи>" (необязательно), "from": "9", "to": "11"}, ...]
        renamed, errors = [], []
        used = {m.group(1) for t, _ in flat for m in [TITLE_RE.match(t.get("name") or "")] if m}
        for r in rows:
            src, dst, tid = clean(r.get("from")), clean(r.get("to")), clean(r.get("id"))
            cand = [t for t, _ in flat if (m := TITLE_RE.match(t.get("name") or "")) and m.group(1) == src
                    and (not tid or str(t.get("id")) == tid)]
            if len(cand) != 1:
                errors.append("№%s→№%s: найдено задач %d" % (src, dst, len(cand)))
                continue
            if dst in used and dst != src and not any(clean(x.get("from")) == dst for x in rows):
                errors.append("№%s→№%s: номер уже занят" % (src, dst))
                continue
            t = cand[0]
            new = TITLE_RE.sub("КМГ №%s:" % dst, t.get("name"), count=1)
            try:
                ws.call("update_task", id_task=t.get("id"), title=new)
                renamed.append({"id": str(t.get("id")), "from": src, "to": dst, "title": new})
            except W.WSError as e:
                errors.append("№%s→№%s: %s" % (src, dst, str(e)[:150]))
        known = (used - {x["from"] for x in renamed}) | {x["to"] for x in renamed}
        os.makedirs("kmg", exist_ok=True)
        json.dump({"date": today.isoformat(), "mode": mode, "renamed": renamed, "errors": errors,
                   "created": [], "updates": [], "known": sorted(known, key=int)},
                  open(os.path.join("kmg", "result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("Режим: rename\n  переименовано %d\n  ошибок %d" % (len(renamed), len(errors)))
        if errors:
            sys.exit(1)
        return

    want = "поддержка %s %d" % (MONTHS[today.month].lower(), today.year)
    month_task = next((t for t, parent in flat if parent is None
                       and " ".join(norm(t.get("name"))) == want), None)

    users = ws.call("get_users").get("data", []) or []
    korol = next((u for u in users if {"король", "ростислав"} <= set(norm(u.get("name")))), None)
    korol_email = (korol or {}).get("email") or ""

    created, would_create, updates, errors = [], [], [], []
    skipped_ready = 0
    for r in rows:
        n = clean(r.get("A"))
        if not n.isdigit():
            continue
        t = linked.get(n)
        if t is None and not clean(r.get("E")):
            continue
        if t is None:
            if is_ready(r.get("I")):
                skipped_ready += 1
                continue
            desc = clean(r.get("E"))
            first = " ".join(desc.split())
            title = "КМГ №%s: %s" % (n, first[:90].rstrip(" ,.;") + ("…" if len(first) > 90 else ""))
            d1, d2 = xdate(r.get("C")), xdate(r.get("H"))
            text = "\n".join(x for x in [
                "Обращение №%s из реестра задач КазМунайГаз (Backlog.xlsx, лист ERP, строка %s)." % (n, r.get("row")),
                "Система: %s" % clean(r.get("B")) if clean(r.get("B")) else "",
                "Дата обращения: %s" % d1.strftime("%d.%m.%Y") if d1 else "",
                "Контактное лицо: %s" % clean(r.get("D")) if clean(r.get("D")) else "",
                "Приоритет: %s" % clean(r.get("F")) if clean(r.get("F")) else "",
                "Крайний срок: %s" % d2.strftime("%d.%m.%Y") if d2 else "",
                "", "Описание задачи:", desc,
                ("\nКомментарий в реестре: %s" % clean(r.get("M"))) if clean(r.get("M")) else "",
                "", "Реестр: " + SRC,
            ] if x is not None)
            item = {"n": n, "row": r.get("row"), "title": title}
            if mode != "send":
                would_create.append(item)
                continue
            if not month_task:
                errors.append("нет задачи месяца «Поддержка %s %d!»" % (MONTHS[today.month], today.year))
                continue
            if not korol_email:
                errors.append("не найден пользователь Ростислав Король")
                continue
            params = {"id_project": pid, "id_parent": month_task.get("id"), "title": title,
                      "email_user_to": korol_email, "text": text, "tags": "Оплачувана,IN PROGRESS",
                      "priority": PRIO.get(clean(r.get("F")).lower()),
                      "datestart": W.fmt_ws(min(d1, today)) if d1 else W.fmt_ws(today),
                      "dateend": W.fmt_ws(d2) if d2 and d2 >= (d1 or d2) else None}
            try:
                res = ws.call("post_task", **params)
                tid = str((res.get("data") or {}).get("id") or "")
                created.append(dict(item, id=tid, url="https://wecode.worksection.com/task/%s/" % tid))
            except W.WSError as e:
                errors.append("строка №%s: %s" % (n, str(e)[:150]))
            continue
        # задача уже есть: если в Worksection DONE, а в файле ещё не «Готово» — отметить в файле
        stage = W.stage_from_tags(tags_of(t), pay_names).upper()
        closed = str(t.get("status") or "").lower() == "done"
        if (stage.startswith("DONE") or closed) and not is_ready(r.get("I")):
            dd = W.parse_date(t.get("date_closed")) if closed else None
            updates.append({"n": n, "row": r.get("row"), "id": str(t.get("id")),
                            "status": "Готово", "date": (dd or today).strftime("%d.%m.%Y"),
                            "comment": "Необходимо проверить"})

    result = {"date": today.isoformat(), "mode": mode,
              "month_task": ({"id": str(month_task.get("id")), "name": month_task.get("name")} if month_task else None),
              "created": created, "would_create": would_create, "updates": updates, "errors": errors,
              "linked": len(linked), "skipped_ready": skipped_ready,
              # номера строк, по которым задача уже есть: в следующий раз по ним достаточно передать A, I и row
              "known": sorted(set(linked) | {c["n"] for c in created}, key=int)}
    os.makedirs("kmg", exist_ok=True)
    json.dump(result, open(os.path.join("kmg", "result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Режим: %s" % mode)
    print("  строк в файле                    %d" % len(rows))
    print("  уже связаны с задачами           %d" % len(linked))
    print("  задача месяца найдена            %s" % ("да" if month_task else "НЕТ"))
    print("  уже «Готово» в файле (пропуск)   %d" % skipped_ready)
    print("  создать подзадач (dry)           %d" % len(would_create))
    print("  создано подзадач                 %d" % len(created))
    print("  отметить «Готово» в файле        %d" % len(updates))
    print("  ошибок                           %d" % len(errors))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
