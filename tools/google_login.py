"""One-time Google Health API login + data probe, run on your own computer.

1. Finds the OAuth "Desktop app" client JSON (newest ~/Downloads/client_secret_*.json,
   or --client PATH) and keeps a copy in ~/.config/health-pipeline/.
2. Opens the Google consent page, catches the redirect on 127.0.0.1 (PKCE), and stores
   the refresh token in ~/.config/health-pipeline/token.json (mode 600). Tokens are
   never printed.
3. Probes every data type the pipeline needs over the last few days and saves the raw
   API responses to --probe-dir, so the parser can be written against real payloads.

Standard library only; works on the macOS system python3 (3.9).

usage:
  python3 tools/google_login.py                 # login + probe
  python3 tools/google_login.py --probe-only    # reuse the saved token
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import http.server
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, timedelta

API = "https://health.googleapis.com/v4/users/me/dataTypes"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]
CONF_DIR = os.path.expanduser("~/.config/health-pipeline")
CLIENT_PATH = os.path.join(CONF_DIR, "client.json")
TOKEN_PATH = os.path.join(CONF_DIR, "token.json")

# what the probe asks for: (data type, kind, days back)
DAILY = ["daily-heart-rate-variability", "daily-resting-heart-rate", "daily-oxygen-saturation",
         "daily-respiratory-rate", "daily-sleep-temperature-derivations", "daily-vo2-max",
         "daily-heart-rate-zones"]
ROLLUP = ["steps", "distance", "floors", "active-energy-burned", "total-calories",
          "active-zone-minutes", "active-minutes", "heart-rate"]
SESSIONS = {"sleep": "sleep.interval.civil_end_time", "exercise": "exercise.interval.civil_start_time"}
SAMPLES = ["weight", "body-fat", "heart-rate-variability", "respiratory-rate-sleep-summary"]


# ---------------------------------------------------------------- http -------
class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


_use_curl = False


def _curl(method: str, url: str, headers: dict, data: bytes | None) -> tuple[int, str]:
    cmd = ["curl", "-sS", "-X", method, "-w", "\n%{http_code}", url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    p = subprocess.run(cmd, input=data, capture_output=True, check=True)
    out = p.stdout.decode()
    body, _, code = out.rpartition("\n")
    return int(code), body


def request(method: str, url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    """JSON request; falls back to curl if this python has no usable CA bundle."""
    global _use_curl
    headers = dict(headers or {})
    if not _use_curl:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, e.read().decode(errors="replace")) from None
        except urllib.error.URLError as e:
            if not isinstance(e.reason, ssl.SSLError):
                raise
            _use_curl = True  # e.g. CERTIFICATE_VERIFY_FAILED on a bare python install
    status, body = _curl(method, url, headers, data)
    if status >= 400:
        raise HttpError(status, body)
    return json.loads(body or "{}")


# ---------------------------------------------------------------- oauth ------
def find_client(arg: str | None) -> dict:
    path = arg
    if not path:
        cands = sorted(glob.glob(os.path.expanduser("~/Downloads/client_secret_*.json")), key=os.path.getmtime)
        path = cands[-1] if cands else (CLIENT_PATH if os.path.exists(CLIENT_PATH) else None)
    if not path:
        sys.exit("client JSON bulunamadı: ~/Downloads/client_secret_*.json yok. --client ile yolu ver.")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    c = raw.get("installed") or raw.get("web")
    if not c or "client_id" not in c:
        sys.exit(f"{path} bir OAuth istemci dosyası değil.")
    if "installed" not in raw:
        print("uyarı: bu istemci 'Masaüstü uygulaması' değil; yönlendirme hatası alırsan Desktop app oluştur.")
    os.makedirs(CONF_DIR, exist_ok=True)
    if os.path.abspath(path) != CLIENT_PATH:
        shutil.copyfile(path, CLIENT_PATH)
        os.chmod(CLIENT_PATH, 0o600)
    print(f"istemci: {os.path.basename(path)}")
    return c


def login(client: dict) -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            msg = "Tamam, terminale dönebilirsin." if ok else "Giriş başarısız, terminale bak."
            body = f"<html><meta charset='utf-8'><body style='font:20px sans-serif;padding:40px'>{msg}</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    t = threading.Thread(target=lambda: [srv.handle_request() for _ in range(20) if not got], daemon=True)
    t.start()
    print("tarayıcıda Google girişi açılıyor... (açılmazsa bu adresi kopyala)\n" + url + "\n")
    webbrowser.open(url)
    t.join(timeout=600)
    srv.server_close()
    if got.get("error"):
        sys.exit(f"Google girişi reddetti: {got['error']}")
    if "code" not in got or got.get("state") != state:
        sys.exit("giriş tamamlanmadı (10 dk içinde yanıt gelmedi).")

    tok = request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                  urllib.parse.urlencode({
                      "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                      "code": got["code"], "code_verifier": verifier, "grant_type": "authorization_code",
                      "redirect_uri": redirect}).encode())
    if "refresh_token" not in tok:
        sys.exit("Google refresh token vermedi. myaccount.google.com/permissions'tan uygulamayı kaldırıp tekrar dene.")
    granted = set(tok.get("scope", "").split())
    missing = [s.rsplit("googlehealth.", 1)[-1] for s in SCOPES if s not in granted]
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", "")}, f)
    os.chmod(TOKEN_PATH, 0o600)
    print("giriş tamam, token kaydedildi: ~/.config/health-pipeline/token.json")
    if missing:
        print("UYARI: şu izinler verilmedi (onay ekranında kutucukları işaretlemedin): " + ", ".join(missing))


def access_token(client: dict) -> str:
    if not os.path.exists(TOKEN_PATH):
        sys.exit("kayıtlı token yok; önce --probe-only olmadan çalıştır.")
    with open(TOKEN_PATH, encoding="utf-8") as f:
        rt = json.load(f)["refresh_token"]
    try:
        tok = request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                      urllib.parse.urlencode({"client_id": client["client_id"],
                                              "client_secret": client.get("client_secret", ""),
                                              "refresh_token": rt, "grant_type": "refresh_token"}).encode())
    except HttpError as e:
        sys.exit(f"token yenilenemedi ({e.status}): {_err(e.body)} -> tekrar giriş yap.")
    return tok["access_token"]


# ---------------------------------------------------------------- probe ------
def _err(body: str) -> str:
    try:
        e = json.loads(body).get("error", {})
        if isinstance(e, dict):
            return f"{e.get('status', '')} {e.get('message', '')}".strip()[:300]
        return str(e)[:300]
    except ValueError:
        return body[:300]


def _civil(d: date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day},
            "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0}}


def list_points(tok: str, dtype: str, flt: str, page_size: int) -> list:
    out, page = [], None
    for _ in range(20):
        q = {"filter": flt, "pageSize": page_size}
        if page:
            q["pageToken"] = page
        r = request("GET", f"{API}/{dtype}/dataPoints?" + urllib.parse.urlencode(q),
                    {"Authorization": f"Bearer {tok}"})
        out.extend(r.get("dataPoints", []))
        page = r.get("nextPageToken")
        if not page:
            break
    return out


def daily_rollup(tok: str, dtype: str, start: date, end: date) -> list:
    body = {"range": {"start": _civil(start), "end": _civil(end)}, "windowSizeDays": 1}
    r = request("POST", f"{API}/{dtype}/dataPoints:dailyRollUp",
                {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json.dumps(body).encode())
    return r.get("rollupDataPoints", [])


def probe(client: dict, out_dir: str, days: int) -> None:
    tok = access_token(client)
    os.makedirs(out_dir, exist_ok=True)
    today = date.today()
    start, end = today - timedelta(days=days), today + timedelta(days=1)
    s, e = start.isoformat(), end.isoformat()
    s30 = (today - timedelta(days=30)).isoformat()
    jobs = []
    for t in DAILY:
        f = t.replace("-", "_")
        jobs.append((t, lambda t=t, f=f: list_points(tok, t, f'{f}.date >= "{s}" AND {f}.date < "{e}"', 1000)))
    for t, field in SESSIONS.items():
        jobs.append((t, lambda t=t, field=field: list_points(
            tok, t, f'{field} >= "{s}T00:00:00" AND {field} < "{e}T00:00:00"', 25)))
    for t in SAMPLES:
        f = t.replace("-", "_")
        since = s30 if t in ("weight", "body-fat") else s
        jobs.append((t, lambda t=t, f=f, since=since: list_points(
            tok, t, f'{f}.sample_time.civil_time >= "{since}T00:00:00"', 1000)))
    for t in ROLLUP:
        jobs.append((t + ".rollup", lambda t=t: daily_rollup(tok, t, start, end)))

    print(f"\nprobe: son {days} gün (kilo/yağ 30 gün) -> {out_dir}")
    summary = {}
    for name, fn in jobs:
        path = os.path.join(out_dir, name + ".json")
        try:
            pts = fn()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(pts, fh, ensure_ascii=False, indent=1)
            summary[name] = len(pts)
            print(f"  {name:40s} {len(pts):5d} kayıt")
        except HttpError as ex:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"http_status": ex.status, "error": _err(ex.body)}, fh, ensure_ascii=False, indent=1)
            summary[name] = f"HTTP {ex.status}"
            print(f"  {name:40s} HTTP {ex.status}: {_err(ex.body)[:90]}")
    with open(os.path.join(out_dir, "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": today.isoformat(), "days": days, "results": summary}, fh, indent=1)
    print("\nbitti. Claude'a 'probe tamam' yaz.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", help="OAuth client JSON (default: newest ~/Downloads/client_secret_*.json)")
    ap.add_argument("--probe-only", action="store_true", help="skip login, reuse saved token")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--probe-dir", default=os.path.expanduser("~/Projects/google-probe"))
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    client = find_client(a.client)
    if not a.probe_only:
        login(client)
    if not a.no_probe:
        probe(client, a.probe_dir, a.days)


if __name__ == "__main__":
    main()
