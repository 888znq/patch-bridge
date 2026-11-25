import os, re, time, hmac
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
API_SECRET = os.environ["API_SECRET"]

_tok = {"v": None, "exp": 0}

def get_access_token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=10)
    r.raise_for_status()
    j = r.json()
    _tok["v"] = j["access_token"]
    _tok["exp"] = time.time() + j.get("expires_in", 3600)
    return _tok["v"]

def authed():
    got = request.headers.get("X-Secret") or ""
    return hmac.compare_digest(got, API_SECRET)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/pull")
def pull():
    if not authed():
        return jsonify({"error": "unauthorized"}), 401
    repo = request.args.get("repo", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", repo):
        return jsonify({"error": "bad repo"}), 400
    try:
        h = {"Authorization": f"Bearer {get_access_token()}"}
        q = (f"name = 'PATCH__{repo}__index.html' and "
             "mimeType = 'application/vnd.google-apps.document' and trashed = false")
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers=h,
                         params={"q": q, "fields": "files(id,name)",
                                 "orderBy": "modifiedTime desc", "pageSize": 1},
                         timeout=10)
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            return jsonify({"found": False})
        fid = files[0]["id"]
        ex = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}/export",
                          headers=h, params={"mimeType": "text/plain"}, timeout=10)
        ex.raise_for_status()
        txt = ex.content.decode("utf-8-sig").replace("\r\n", "\n").strip()
        if not txt:
            return jsonify({"found": False})
        return jsonify({"found": True, "id": fid, "repo": repo, "patch": txt})
    except requests.RequestException as e:
        return jsonify({"error": f"upstream: {e}"}), 502

@app.route("/ack", methods=["POST"])
def ack():
    if not authed():
        return jsonify({"error": "unauthorized"}), 401
    fid = request.args.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid):
        return jsonify({"error": "bad id"}), 400
    try:
        h = {"Authorization": f"Bearer {get_access_token()}"}
        m = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                         headers=h, params={"fields": "name"}, timeout=10)
        m.raise_for_status()
        if not m.json().get("name", "").startswith("PATCH__"):
            return jsonify({"error": "not a patch doc"}), 403
        t = requests.patch(f"https://www.googleapis.com/drive/v3/files/{fid}",
                           headers={**h, "Content-Type": "application/json"},
                           json={"trashed": True}, timeout=10)
        t.raise_for_status()
        return jsonify({"ok": True})
    except requests.RequestException as e:
        return jsonify({"error": f"upstream: {e}"}), 502
