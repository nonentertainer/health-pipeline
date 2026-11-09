"""Generate the 'Sağlık → GitHub' Shortcut from a reference Shortcut the user built by hand.

Every action dict is derived from the user's own working actions (same identifiers,
same parameter shapes); only types, variable names and wiring change. The output is an
unsigned binary plist that macOS `shortcuts sign` turns into an importable file.

usage: python3 build_shortcut.py ref.plist out.shortcut
"""

from __future__ import annotations

import copy
import plistlib
import sys
import uuid

OBJ = "￼"  # attachment placeholder character Shortcuts uses inside text tokens

# daily metrics: (payload key, Apple Health type name as Shortcuts stores it)
# Ordered most-certain first; the last three names are the least certain.
METRICS = [
    ("steps", "Steps"),
    ("kcal", "Active Energy"),
    ("basal_energy", "Resting Energy"),
    ("distance", "Walking + Running Distance"),
    ("exercise", "Exercise Minutes"),
    ("flights", "Flights Climbed"),
    ("hrv", "Heart Rate Variability"),
    ("rhr", "Resting Heart Rate"),
    ("hr", "Heart Rate"),
    ("walking_hr", "Walking Heart Rate Average"),
    ("resp", "Respiratory Rate"),
    ("kilo", "Weight"),
    ("body_fat", "Body Fat Percentage"),
    ("lean_mass", "Lean Body Mass"),
    ("spo2", "Blood Oxygen"),
    ("temp", "Wrist Temperature"),
    ("vo2max", "Cardio Fitness"),
]
DAYS_BACK = "3"
SLEEP_DAYS_BACK = "2"


def U() -> str:
    return str(uuid.uuid4()).upper()


def find(ref_actions, ident):
    for a in ref_actions:
        if a["WFWorkflowActionIdentifier"] == ident:
            return a
    raise SystemExit(f"reference is missing {ident}")


def var_token(name: str, prop: str | None = None) -> dict:
    att = {"Type": "Variable", "VariableName": name}
    if prop:
        att["Aggrandizements"] = [{"PropertyName": prop, "Type": "WFPropertyVariableAggrandizement"}]
    return att


def out_token(name: str, uid: str) -> dict:
    return {"OutputName": name, "OutputUUID": uid, "Type": "ActionOutput"}


def text_string(parts: list) -> dict:
    """parts: plain strings and attachment dicts -> WFTextTokenString."""
    s, atts = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            atts[f"{{{len(s)}, 1}}"] = p
            s += OBJ
    val = {"string": s}
    if atts:
        val["attachmentsByRange"] = atts
    return {"Value": val, "WFSerializationType": "WFTextTokenString"}


def attachment(att: dict) -> dict:
    return {"Value": att, "WFSerializationType": "WFTextTokenAttachment"}


class Builder:
    def __init__(self, ref: dict):
        acts = ref["WFWorkflowActions"]
        self.t_find = find(acts, "is.workflow.actions.filter.health.quantity")
        self.t_repeat = find(acts, "is.workflow.actions.repeat.each")
        self.t_date = find(acts, "is.workflow.actions.format.date")
        self.t_text = find(acts, "is.workflow.actions.gettext")
        self.t_combine = find(acts, "is.workflow.actions.text.combine")
        self.t_setvar = find(acts, "is.workflow.actions.setvariable")
        self.t_url = find(acts, "is.workflow.actions.downloadurl")
        self.out: list[dict] = []

    def _act(self, tmpl: dict, params: dict) -> dict:
        a = {"WFWorkflowActionIdentifier": tmpl["WFWorkflowActionIdentifier"], "WFWorkflowActionParameters": params}
        self.out.append(a)
        return a

    def find_samples(self, hk_type: str, days: str, group_day: bool) -> str:
        p = copy.deepcopy(self.t_find["WFWorkflowActionParameters"])
        uid = U()
        p["UUID"] = uid
        tpl = p["WFContentItemFilter"]["Value"]["WFActionParameterFilterTemplates"]
        type_f = copy.deepcopy(next(t for t in tpl if t["Property"] == "Type"))
        date_f = copy.deepcopy(next(t for t in tpl if t["Property"] == "Start Date"))
        type_f["Values"]["Enumeration"]["Value"] = hk_type
        date_f["Values"]["Number"] = days
        p["WFContentItemFilter"]["Value"]["WFActionParameterFilterTemplates"] = [type_f, date_f]
        p["WFContentItemLimitEnabled"] = False
        if group_day:
            p["WFHKSampleFilteringGroupBy"] = "Day"
        else:
            p.pop("WFHKSampleFilteringGroupBy", None)
        self._act(self.t_find, p)
        return uid

    def repeat_start(self, input_uid: str) -> str:
        gid = U()
        self._act(self.t_repeat, {
            "GroupingIdentifier": gid, "WFControlFlowMode": 0,
            "WFInput": attachment(out_token("Sağlık Örnekleri", input_uid)),
        })
        return gid

    def repeat_end(self, gid: str) -> str:
        uid = U()
        self._act(self.t_repeat, {"GroupingIdentifier": gid, "UUID": uid, "WFControlFlowMode": 2})
        return uid

    def format_date(self, prop: str | None, fmt: str) -> str:
        p = copy.deepcopy(self.t_date["WFWorkflowActionParameters"])
        uid = U()
        p["UUID"] = uid
        p["WFDate"] = text_string([var_token("Repeat Item", prop)])
        p["WFDateFormat"] = fmt
        p["WFDateFormatStyle"] = "Custom"
        self._act(self.t_date, p)
        return uid

    def text(self, parts: list) -> str:
        uid = U()
        self._act(self.t_text, {"UUID": uid, "WFTextActionText": text_string(parts)})
        return uid

    def combine(self, results_uid: str) -> str:
        p = copy.deepcopy(self.t_combine["WFWorkflowActionParameters"])
        uid = U()
        p.update({"UUID": uid, "WFTextCustomSeparator": ";", "WFTextSeparator": "Custom",
                  "text": attachment(out_token("Yineleme Sonuçları", results_uid))})
        self._act(self.t_combine, p)
        return uid

    def set_var(self, name: str, from_uid: str) -> None:
        self._act(self.t_setvar, {"WFInput": attachment(out_token("Birleştirilmiş Metin", from_uid)),
                                  "WFVariableName": name})

    # ---- blocks -------------------------------------------------------------
    def metric_block(self, key: str, hk_type: str) -> None:
        found = self.find_samples(hk_type, DAYS_BACK, group_day=True)
        gid = self.repeat_start(found)
        d = self.format_date(None, "yyyy-MM-dd")
        self.text([out_token("Biçimlendirilmiş Tarih", d), "=", var_token("Repeat Item")])
        end = self.repeat_end(gid)
        self.set_var(key, self.combine(end))

    def sleep_block(self) -> None:
        found = self.find_samples("Sleep", SLEEP_DAYS_BACK, group_day=False)
        gid = self.repeat_start(found)
        s = self.format_date("Start Date", "yyyy-MM-dd'T'HH:mm")
        e = self.format_date("End Date", "yyyy-MM-dd'T'HH:mm")
        self.text([out_token("Biçimlendirilmiş Tarih", s), "|", out_token("Biçimlendirilmiş Tarih", e), "|",
                   var_token("Repeat Item", "Value")])
        end = self.repeat_end(gid)
        self.set_var("sleep", self.combine(end))

    def send(self) -> None:
        p = copy.deepcopy(self.t_url["WFWorkflowActionParameters"])
        p["UUID"] = U()

        def kv(key: str, value: dict, item_type: int = 0) -> dict:
            return {"WFItemType": item_type, "WFKey": text_string([key]), "WFValue": value}

        def dict_value(items: list) -> dict:
            return {"Value": {"WFDictionaryFieldValueItems": items}, "WFSerializationType": "WFDictionaryFieldValue"}

        metrics = [kv(k, text_string([var_token(k)])) for k, _ in METRICS]
        payload = [
            kv("source", text_string(["shortcut"])),
            kv("metrics", {"Value": dict_value(metrics), "WFSerializationType": "WFDictionaryFieldValue"}, 1),
            kv("sleep", text_string([var_token("sleep")])),
        ]
        p["WFJSONValues"] = dict_value([
            kv("event_type", text_string(["health"])),
            kv("client_payload", {"Value": dict_value(payload), "WFSerializationType": "WFDictionaryFieldValue"}, 1),
        ])
        self._act(self.t_url, p)


def build(ref: dict) -> dict:
    b = Builder(ref)
    for key, hk in METRICS:
        b.metric_block(key, hk)
    b.sleep_block()
    b.send()
    out = {k: copy.deepcopy(v) for k, v in ref.items() if k != "WFWorkflowActions"}
    out["WFWorkflowActions"] = b.out
    out["WFWorkflowImportQuestions"] = []
    out["WFWorkflowInputContentItemClasses"] = ref.get("WFWorkflowInputContentItemClasses", [])
    return out


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as f:
        ref = plistlib.load(f)
    wf = build(ref)
    with open(dst, "wb") as f:
        plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
    print(f"{len(wf['WFWorkflowActions'])} actions -> {dst}")


if __name__ == "__main__":
    main()
