import os, re, time, hmac, base64, threading
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
API_SECRET = os.environ["API_SECRET"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
OWNER = os.environ.get("GITHUB_OWNER", "888znq")
REPOS = [r.strip() for r in os.environ.get("REPOS", "MTG").split(",") if r.strip()]
SETTLE_SECONDS = 10   # ignore Docs modified in the last 10s (Gemini may still be writing)
MAX_PER_TICK = 5

_tok = {"v": None, "exp": 0}
_lock = threading.Lock()
_log = []
_done = {}   # doc id -> modifiedTime, for pushed patches whose Doc could not be trashed

def log(msg):
    _log.append(f"{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')} {msg}")
    del _log[:-50]

def get_access_token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token",
    }, timeout=10)
    r.raise_for_status()
    j = r.json()
    _tok["v"] = j["access_token"]
    _tok["exp"] = time.time() + j.get("expires_in", 3600)
    return _tok["v"]

def authed():
    got = request.headers.get("X-Secret") or ""
    return hmac.compare_digest(got.encode(), API_SECRET.encode())

# ---------- GitHub ----------
def gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def gh_url(repo):
    return f"https://api.github.com/repos/{OWNER}/{repo}/contents/index.html"

def gh_get(repo, ref=None):
    r = requests.get(gh_url(repo), headers=gh_headers(),
                     params={"ref": ref} if ref else None, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub GET {r.status_code}")
    d = r.json()
    if not d.get("content") or d.get("encoding") != "base64":
        raise RuntimeError("index.html too large for contents API (>1MB)")
    return d["sha"], base64.b64decode(d["content"]).decode("utf-8")

def gh_put(repo, text, sha, message):
    r = requests.put(gh_url(repo), headers=gh_headers(), timeout=20, json={
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode(),
        "sha": sha})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PUT {r.status_code}")
    return r.json()["commit"]["sha"]

# ---------- patching ----------
def apply_patch(original, patch_text):
    crlf = "\r\n" in original
    html = original.replace("\r\n", "\n")
    chunks = [c.strip() for c in patch_text.replace("\r\n", "\n").split("===NEXT===") if c.strip()]
    if not chunks:
        raise ValueError("empty patch")
    for i, hunk in enumerate(chunks, 1):
        idx = hunk.find("===SPLIT===")
        if idx < 0:
            raise ValueError(f"hunk {i}: no ===SPLIT===")
        old, new = hunk[:idx].strip(), hunk[idx + 11:].strip()
        if not old:
            raise ValueError(f"hunk {i}: OLD block empty")
        n = html.count(old)
        if n == 0:
            raise ValueError(f"hunk {i}: OLD block not found")
        if n > 1:
            raise ValueError(f"hunk {i}: OLD block ambiguous ({n} matches)")
        html = html.replace(old, new, 1)
    return html.replace("\n", "\r\n") if crlf else html

def sanity(original, result):
    if result == original:
        raise ValueError("patch changes nothing")
    if not result.strip():
        raise ValueError("result is empty")
    if len(result) < 0.5 * len(original):
        raise ValueError("patch shrinks the file by more than 50%")
    if "</html>" in original.lower() and "</html>" not in result.lower():
        raise ValueError("patch removed </html>")

# ---------- Drive ----------
DRIVE = "https://www.googleapis.com/drive/v3/files"

def drive_find(h):
    names = " or ".join(f"name = 'PATCH__{r}__index.html'" for r in REPOS)
    q = f"({names}) and mimeType = 'application/vnd.google-apps.document' and trashed = false"
    r = requests.get(DRIVE, headers=h, timeout=10, params={
        "q": q, "fields": "files(id,name,modifiedTime)", "orderBy": "modifiedTime", "pageSize": 20})
    r.raise_for_status()
    return r.json().get("files", [])

def drive_trash(h, fid):
    requests.patch(f"{DRIVE}/{fid}", headers={**h, "Content-Type": "application/json"},
                   json={"trashed": True}, timeout=10).raise_for_status()

def drive_mark_failed(h, fid, name, reason):
    requests.patch(f"{DRIVE}/{fid}", headers={**h, "Content-Type": "application/json"},
                   json={"name": "FAILED__" + name, "description": reason[:300]}, timeout=10).raise_for_status()

def process(h, f):
    fid, name = f["id"], f["name"]
    m = re.fullmatch(r"PATCH__([A-Za-z0-9_.-]{1,40})__index\.html", name)
    if not m or m.group(1) not in REPOS:
        return "skipped"
    repo = m.group(1)
    if _done.get(fid) == f["modifiedTime"]:
        drive_trash(h, fid); _done.pop(fid, None)
        return "cleaned"
    age = (datetime.now(timezone.utc) -
           datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))).total_seconds()
    if age < SETTLE_SECONDS:
        return "settling"
    ex = requests.get(f"{DRIVE}/{fid}/export", headers=h, params={"mimeType": "text/plain"}, timeout=10)
    ex.raise_for_status()
    patch = ex.content.decode("utf-8-sig").replace("\r\n", "\n").strip()
    if not patch:
        return "empty"
    try:
        sha, original = gh_get(repo)
        result = apply_patch(original, patch)
        sanity(original, result)
    except ValueError as e:
        drive_mark_failed(h, fid, name, str(e))
        log(f"{repo}: REJECTED {e}")
        return "rejected"
    commit = gh_put(repo, result, sha, f"Gemini patch via patch-bridge")
    log(f"{repo}: pushed {commit[:7]}")
    try:
        drive_trash(h, fid)
    except requests.RequestException:
        _done[fid] = f["modifiedTime"]      # never re-apply this patch
        log(f"{repo}: pushed but could not trash Doc, will retry")
    return "pushed"

# ---------- routes ----------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/tick", methods=["GET", "POST"])
def tick():
    if not authed():
        return jsonify({"error": "unauthorized"}), 401
    if not _lock.acquire(blocking=False):
        return jsonify({"busy": True})
    try:
        h = {"Authorization": f"Bearer {get_access_token()}"}
        files = drive_find(h)
        out, errors = [], 0
        for f in files[:MAX_PER_TICK]:
            try:
                out.append([f["name"], process(h, f)])
            except (RuntimeError, requests.RequestException) as e:
                errors += 1
                log(f"{f['name']}: ERROR {e}")
                out.append([f["name"], "error"])
        return jsonify({"checked": len(files), "results": out}), (502 if errors else 200)
    except requests.RequestException as e:
        log(f"tick upstream error: {e}")
        return jsonify({"error": "upstream"}), 502
    finally:
        _lock.release()

@app.route("/log")
def show_log():
    if not authed():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"log": _log})

@app.route("/undo", methods=["POST"])
def undo():
    if not authed():
        return jsonify({"error": "unauthorized"}), 401
    repo = request.args.get("repo", "")
    if repo not in REPOS:
        return jsonify({"error": "unknown repo"}), 400
    try:
        r = requests.get(f"https://api.github.com/repos/{OWNER}/{repo}/commits",
                         headers=gh_headers(), params={"path": "index.html", "per_page": 2}, timeout=15)
        r.raise_for_status()
        commits = r.json()
        if len(commits) < 2:
            return jsonify({"error": "no earlier version"}), 400
        prev = commits[1]["sha"]
        _, old_text = gh_get(repo, ref=prev)
        cur_sha, _ = gh_get(repo)
        c = gh_put(repo, old_text, cur_sha, f"Undo: restore index.html from {prev[:7]}")
        log(f"{repo}: undo -> {c[:7]}")
        return jsonify({"ok": True, "restored_from": prev[:7]})
    except (RuntimeError, requests.RequestException) as e:
        return jsonify({"error": str(e)}), 502
