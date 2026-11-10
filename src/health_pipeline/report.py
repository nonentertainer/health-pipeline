"""Weekly report: one ISO week in a few numbers and plain sentences.

    health-pipeline report --data-dir data [--log LOG] [--week 2026-W45] \
        --out reports/2026-W45.md [--json report.json] [--lang tr|en]

Inputs are the sync's tables (``daily.csv``, ``workouts.csv``, ``insights.json``) and,
optionally, the person's own log: the documents the dashboard stores per day
(``{"date", "weight", "waist", "sessions": [...]}``), given as one JSON list or as a
folder of one-document JSON files (the shape a database export writes). The log is
where weigh-ins and lifted weights live; without it those parts are left out.

A training day counts once per kind, whether it was logged by hand or only detected
by the watch: strength from a logged session or a 15+ minute weights workout, cardio
from a logged run/ride or a 15+ minute run, ride or cross-trainer workout. Walks count
as minutes, not as training.

"Next week" suggestions are fixed rules over these numbers, in priority order, at most
three. They state what the numbers say; they do not guess at causes.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
from datetime import date, timedelta

from .insights import clock, onset_minutes

TRAINING_MIN = 15
STRENGTH = {"gym", "full", "lower", "upper"}
CARDIO = {"run", "bike"}
FIT_KIND = (("treadmill", "cardio"), ("run", "cardio"), ("ellip", "cardio"), ("bike", "cardio"),
            ("cycl", "cardio"), ("spin", "cardio"), ("swim", "cardio"), ("weight", "gym"),
            ("strength", "gym"), ("walk", "walk"), ("hike", "walk"))


# ---------- weeks ----------

def week_of(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[date, date]:
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday, monday + timedelta(days=6)


def last_full_week(today: date) -> str:
    return week_of(today - timedelta(days=today.isoweekday()))


# ---------- inputs ----------

def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_log(path: str | None) -> dict[str, dict]:
    """date -> day document. Accepts a JSON list/object, or a folder of JSON files."""
    if not path or not os.path.exists(path):
        return {}
    docs = []
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for name in sorted(files):
                if name.endswith(".json"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        docs.append(json.load(f))
    else:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        docs = loaded if isinstance(loaded, list) else list(loaded.values()) if isinstance(loaded, dict) else []
    out = {}
    for d in docs:
        d = d.get("data", d) if isinstance(d, dict) else None      # export files wrap the document
        if isinstance(d, dict) and isinstance(d.get("date"), str):
            out[d["date"]] = d
    return out


# ---------- the week ----------

def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=k) for k in range((b - a).days + 1)]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _r(x, n=1):
    return None if x is None else round(x, n)


def training(days: list[date], log: dict, workouts: list[dict]) -> dict:
    kinds: dict[date, set] = {d: set() for d in days}
    walk = 0.0
    for d in days:
        for s in (log.get(d.isoformat()) or {}).get("sessions") or []:
            k = s.get("kind")
            if k in STRENGTH:
                kinds[d].add("gym")
            elif k in CARDIO:
                kinds[d].add("cardio")
            elif k == "walk":
                walk += _num(s.get("min")) or 0
    for w in workouts:
        try:
            d = date.fromisoformat(w.get("date", ""))
        except ValueError:
            continue
        if d not in kinds:
            continue
        name, dur = (w.get("name") or "").lower(), _num(w.get("duration_min")) or 0
        kind = next((k for key, k in FIT_KIND if key in name), None)
        if kind == "walk":
            walk += dur
        elif kind and dur >= TRAINING_MIN:
            kinds[d].add(kind)
    return {"gym": sum("gym" in k for k in kinds.values()), "cardio": sum("cardio" in k for k in kinds.values()),
            "days": sum(bool(k & {"gym", "cardio"}) for k in kinds.values()), "walkMin": round(walk)}


def strength(a: date, b: date, log: dict) -> list[dict]:
    """Per exercise: heaviest logged weight before the week, and in the week."""
    before, during, order = {}, {}, []
    for ds in sorted(log):
        d = date.fromisoformat(ds)
        if d > b:
            continue
        for s in log[ds].get("sessions") or []:
            if s.get("kind") not in STRENGTH:
                continue
            for e in s.get("ex") or []:
                name, kg = (e.get("name") or "").strip(), _num(e.get("kg"))
                if not name or kg is None:
                    continue
                key = name.lower()
                if d < a:
                    before[key] = (name, kg)                  # dates ascend: the last one is the latest
                else:
                    during[key] = (name, max(kg, during.get(key, (name, 0.0))[1]))
                    if key not in order:
                        order.append(key)
    return [{"name": during[k][0], "from": before[k][1] if k in before else None, "to": during[k][1]} for k in order]


def build(data_dir: str, week: str, log: dict | None = None, target: int = 5) -> dict:
    log = log or {}
    a, b = week_bounds(week)
    days = _days(a, b)
    daily = {r["date"]: r for r in _read_csv(os.path.join(data_dir, "daily.csv"))}
    workouts = _read_csv(os.path.join(data_dir, "workouts.csv"))
    col = lambda d, c: _num((daily.get(d.isoformat()) or {}).get(c))

    def weights(x: date, y: date):
        out = [_num((log.get(d.isoformat()) or {}).get("weight")) for d in _days(x, y)]
        if not any(v is not None for v in out):                   # no log: the tables may have a scale
            out = [col(d, "weight_kg") for d in _days(x, y)]
        return [v for v in out if v is not None]

    w_now, w_prev = weights(a, b), weights(a - timedelta(days=7), a - timedelta(days=1))
    weight = None
    if w_now:
        weight = {"avg": _r(_mean(w_now), 2), "n": len(w_now), "prevAvg": _r(_mean(w_prev), 2) if w_prev else None}
        weight["change"] = _r(weight["avg"] - weight["prevAvg"], 2) if weight["prevAvg"] is not None else None

    nights = [(d, onset_minutes((daily.get(d.isoformat()) or {}).get("sleep_start", ""), d), col(d, "sleep_min"))
              for d in days]
    nights = [(d, o, m) for d, o, m in nights if m is not None]
    sleep = None
    if nights:
        onsets = [o for _, o, _ in nights if o is not None]
        sleep = {"nights": len(nights), "avgMin": round(statistics.fmean(m for _, _, m in nights)),
                 "short": sum(m < 360 for _, _, m in nights),
                 "onset": clock(statistics.fmean(onsets)) if onsets else None,
                 "spreadMin": round(statistics.pstdev(onsets)) if len(onsets) > 2 else None}

    prev28 = _days(a - timedelta(days=28), a - timedelta(days=1))
    rec = {"hrv": _r(_mean(col(d, "hrv_ms") for d in days)), "hrvPrev": _r(_mean(col(d, "hrv_ms") for d in prev28)),
           "sleepHr": _r(_mean(col(d, "sleep_hr_bpm") for d in days)),
           "sleepHrPrev": _r(_mean(col(d, "sleep_hr_bpm") for d in prev28)), "alerts": []}
    ins_path = os.path.join(data_dir, "insights.json")
    top = None
    if os.path.exists(ins_path):
        with open(ins_path, encoding="utf-8") as f:
            ins = json.load(f)
        rec["alerts"] = [x["date"] for x in (ins.get("alerts") or {}).get("flagged", [])
                         if a.isoformat() <= x.get("date", "") <= b.isoformat()]
        hrv_rows = [x for x in ins.get("drivers", []) if x.get("y") == "hrv"]
        if hrv_rows:
            top = {k: hrv_rows[0].get(k) for k in ("x", "effect", "verdict", "n", "needN")}

    act = {"stepsAvg": _r(_mean(col(d, "steps") for d in days), 0),
           "activeKcalAvg": _r(_mean(col(d, "active_kcal") for d in days), 0)}
    rep = {"week": week, "from": a.isoformat(), "to": b.isoformat(), "target": target,
           "training": training(days, log, workouts), "strength": strength(a, b, log),
           "weight": weight, "sleep": sleep, "recovery": rec, "activity": act, "finding": top}
    rep["focus"] = focus(rep)
    return rep


def focus(rep: dict) -> list[dict]:
    """Fixed rules, most important first; at most three."""
    out = []
    rec, sl, tr, w = rep["recovery"], rep["sleep"], rep["training"], rep["weight"]
    if rec["alerts"]:
        out.append({"k": "alert", "dates": rec["alerts"]})
    if tr["days"] < rep["target"]:
        out.append({"k": "train", "n": tr["days"], "target": rep["target"]})
    if sl and sl["spreadMin"] is not None and sl["spreadMin"] > 45:
        out.append({"k": "bedtime", "spread": sl["spreadMin"]})
    if sl and sl["short"] >= 2:
        out.append({"k": "short", "n": sl["short"]})
    if w and w["change"] is not None and w["change"] > 0.2:
        out.append({"k": "weight_up", "change": w["change"]})
    return out[:3] or [{"k": "steady"}]


# ---------- words ----------

T = {
    "tr": {"title": "Hafta {week} · {a} – {b}", "train": "Antrenman", "weight": "Kilo", "sleep": "Uyku",
           "rec": "Toparlanma", "str": "Güç", "next": "Gelecek hafta", "act": "Hareket",
           "train_l": "{days}/{target} antrenman günü: {gym} ağırlık, {cardio} kardiyo. Yürüyüş {walk} dk.",
           "weight_l": "Haftalık ortalama {avg} kg ({n} tartı){chg}.", "weight_c": ", önceki haftaya göre {c} kg",
           "sleep_l": "{n} gecede ortalama {dur}; uykuya dalış ort. {onset}{spread}. 6 saatin altı: {short} gece.",
           "spread": ", gece gece ±{s} dk", "rec_l": "HRV ort. {hrv} ms (önceki 4 hafta {hrvp}); uyku nabzı {hr} bpm (önceki {hrp}).",
           "act_l": "Günde ort. {steps} adım, {kcal} aktif kcal.",
           "str_l": "{name}: {frm} → {to} kg", "str_new": "{name}: {to} kg (ilk kayıt)",
           "find_l": "Bulgu: {x} için fark {e} ms, hüküm: {v}.",
           "verdict": {"clear": "net", "likely": "olası", "unclear": "belirsiz"},
           "x": {"bedtime": "geç yatış", "sleep": "uzun uyku", "steps": "önceki gün çok adım",
                 "training": "önceki gün antrenman", "shift": "düzensiz yatış"},
           "alert": "Hastalık uyarısı çaldı: {d}. Toparlanmayı izle, yüklenmeyi ona göre ayarla.",
           "train_f": "Antrenman {n}/{t} günde kaldı. Haftanın başında planlı günleri sabitle.",
           "bedtime": "Yatış saati gece gece ±{s} dk oynadı. Hep aynı saate yakın yatmayı dene.",
           "short": "{n} gece 6 saatin altında uyudun. Uyku süresini öne al.",
           "weight_up": "Ortalama kilo {c} kg arttı. Tek hafta su ve tuzdan olabilir; iki hafta sürerse kaloriye bak.",
           "steady": "Rakamlar yolunda. Aynı düzenle devam.", "none": "veri yok"},
    "en": {"title": "Week {week} · {a} – {b}", "train": "Training", "weight": "Weight", "sleep": "Sleep",
           "rec": "Recovery", "str": "Strength", "next": "Next week", "act": "Activity",
           "train_l": "{days}/{target} training days: {gym} strength, {cardio} cardio. Walking {walk} min.",
           "weight_l": "Weekly average {avg} kg ({n} weigh-ins){chg}.", "weight_c": ", {c} kg vs the week before",
           "sleep_l": "{n} nights, average {dur}; asleep at {onset} on average{spread}. Nights under 6 h: {short}.",
           "spread": ", ±{s} min night to night", "rec_l": "HRV avg {hrv} ms (previous 4 weeks {hrvp}); sleeping HR {hr} bpm (previous {hrp}).",
           "act_l": "{steps} steps and {kcal} active kcal a day on average.",
           "str_l": "{name}: {frm} → {to} kg", "str_new": "{name}: {to} kg (first log)",
           "find_l": "Finding: {x} differs by {e} ms, verdict: {v}.",
           "verdict": {"clear": "clear", "likely": "likely", "unclear": "unclear"},
           "x": {"bedtime": "later bedtime", "sleep": "longer sleep", "steps": "more steps the day before",
                 "training": "training the day before", "shift": "irregular bedtime"},
           "alert": "The illness rule fired: {d}. Watch recovery and adjust the load.",
           "train_f": "Training reached {n}/{t} days. Fix the planned days at the start of the week.",
           "bedtime": "Bedtime moved ±{s} min night to night. Aim for the same time every night.",
           "short": "{n} nights under 6 hours. Move sleep earlier.",
           "weight_up": "Average weight rose {c} kg. One week can be water and salt; if it lasts two, look at calories.",
           "steady": "The numbers are on track. Keep the same routine.", "none": "no data"},
}


def _hm(m) -> str:
    return f"{int(m) // 60}:{int(m) % 60:02d}"


def _s(x, nd=1) -> str:
    if x is None:
        return "–"
    v = f"{abs(x):.{nd}f}"
    return ("+" if x > 0 else "−" if x < 0 else "") + v


def _f(x) -> str:
    return "–" if x is None else f"{x:g}"


def sections(rep: dict, lang: str = "tr") -> dict:
    t = T[lang]
    tr = rep["training"]
    out = {t["train"]: [t["train_l"].format(days=tr["days"], target=rep["target"], gym=tr["gym"],
                                            cardio=tr["cardio"], walk=tr["walkMin"])]}
    w = rep["weight"]
    if w:
        chg = t["weight_c"].format(c=_s(w["change"], 2)) if w["change"] is not None else ""
        out[t["weight"]] = [t["weight_l"].format(avg=_f(w["avg"]), n=w["n"], chg=chg)]
    sl = rep["sleep"]
    if sl:
        spread = t["spread"].format(s=sl["spreadMin"]) if sl["spreadMin"] is not None else ""
        out[t["sleep"]] = [t["sleep_l"].format(n=sl["nights"], dur=_hm(sl["avgMin"]), onset=sl["onset"] or "–",
                                               spread=spread, short=sl["short"])]
    rec = rep["recovery"]
    if rec["hrv"] is not None:
        lines = [t["rec_l"].format(hrv=_f(rec["hrv"]), hrvp=_f(rec["hrvPrev"]), hr=_f(rec["sleepHr"]),
                                   hrp=_f(rec["sleepHrPrev"]))]
        f = rep.get("finding")
        if f:
            lines.append(t["find_l"].format(x=t["x"].get(f["x"], f["x"]), e=_s(f["effect"]), v=t["verdict"][f["verdict"]]))
        out[t["rec"]] = lines
    act = rep["activity"]
    if act["stepsAvg"] is not None:
        out[t["act"]] = [t["act_l"].format(steps=f"{int(act['stepsAvg']):,}".replace(",", "." if lang == "tr" else ","),
                                           kcal=_f(act["activeKcalAvg"]))]
    if rep["strength"]:
        out[t["str"]] = [t["str_l"].format(name=x["name"], frm=_f(x["from"]), to=_f(x["to"])) if x["from"] is not None
                         else t["str_new"].format(name=x["name"], to=_f(x["to"])) for x in rep["strength"]]
    nxt = []
    for x in rep["focus"]:
        k = x["k"]
        if k == "alert":
            nxt.append(t["alert"].format(d=", ".join(x["dates"])))
        elif k == "train":
            nxt.append(t["train_f"].format(n=x["n"], t=x["target"]))
        elif k == "bedtime":
            nxt.append(t["bedtime"].format(s=x["spread"]))
        elif k == "short":
            nxt.append(t["short"].format(n=x["n"]))
        elif k == "weight_up":
            nxt.append(t["weight_up"].format(c=_f(x["change"])))
        else:
            nxt.append(t["steady"])
    out[t["next"]] = nxt
    return out


def to_markdown(rep: dict, lang: str = "tr") -> str:
    t = T[lang]
    lines = [f"# {t['title'].format(week=rep['week'], a=rep['from'], b=rep['to'])}", ""]
    for head, items in sections(rep, lang).items():
        lines += [f"## {head}", ""] + [f"- {x}" for x in items] + [""]
    return "\n".join(lines)


def write(rep: dict, out: str, json_out: str | None = None, lang: str = "tr") -> None:
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(to_markdown(rep, lang))
    if json_out:
        os.makedirs(os.path.dirname(json_out) or ".", exist_ok=True)
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump({**rep, "lang": lang, "sections": sections(rep, lang)}, f, ensure_ascii=False, indent=1)
            f.write("\n")
