import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
API_SECRET = os.environ["API_SECRET"]

def get_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/pull")
def pull_patch():
    if request.args.get("secret") != API_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    repo = request.args.get("repo", "").strip()
    if not repo:
        return jsonify({"error": "missing repo"}), 400

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    doc_name = f"PATCH__{repo}__index.html"

    q = f"name = '{doc_name}' and mimeType = 'application/vnd.google-apps.document' and trashed = false"
    res = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": q, "fields": "files(id, name)"},
        timeout=10,
    ).json()

    files = res.get("files", [])
    if not files:
        return jsonify({"found": False})

    file_id = files[0]["id"]

    txt = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
        headers=headers,
        params={"mimeType": "text/plain"},
        timeout=10,
    ).text.lstrip("\ufeff").replace("\r\n", "\n").strip()

    requests.patch(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers={**headers, "Content-Type": "application/json"},
        json={"trashed": True},
        timeout=10,
    )

    return jsonify({"found": True, "repo": repo, "patch": txt})
