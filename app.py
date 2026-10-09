"""EPDW創業記念 抽選会ルーレット — 共有保存サーバー

抽選の設定・結果・景品写真を GitHub リポジトリの専用ブランチ（既定: lottery-data）に保存し、
どの端末から開いても同じデータを返す。Render の無料プランで動かす前提。

環境変数
  GITHUB_TOKEN    必須。lottery リポジトリの Contents を読み書きできるトークン
  GITHUB_REPO     既定 shiratoriemail/lottery
  DATA_BRANCH     既定 lottery-data（無ければ自動で作成）
  ALLOWED_ORIGINS 既定 *（例: https://shiratoriemail.github.io）
"""
import base64
import json
import os
import re
import threading

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPO", "shiratoriemail/lottery")
BRANCH = os.environ.get("DATA_BRANCH", "lottery-data")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
STATE_PATH = "data/state.json"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
CORS(app, origins=ORIGINS)

lock = threading.Lock()
cache = {"loaded": False, "rev": 0, "state": None, "sha": None}
img_cache = {}  # id -> {"data": str, "sha": str}
branch_ready = {"ok": False}


class StoreError(Exception):
    pass


def gh(method, path, **kw):
    r = requests.request(method, API + path, headers=HEADERS, timeout=20, **kw)
    return r


def ensure_branch():
    if branch_ready["ok"]:
        return
    if not TOKEN:
        raise StoreError("GITHUB_TOKEN が設定されていません")
    r = gh("GET", f"/branches/{BRANCH}")
    if r.status_code == 200:
        branch_ready["ok"] = True
        return
    if r.status_code != 404:
        raise StoreError(f"ブランチ確認に失敗しました ({r.status_code})")
    repo = gh("GET", "")
    if repo.status_code != 200:
        raise StoreError(f"リポジトリを読めません ({repo.status_code})。トークンの権限を確認してください")
    base = repo.json()["default_branch"]
    ref = gh("GET", f"/git/ref/heads/{base}")
    if ref.status_code != 200:
        raise StoreError(f"{base} ブランチを読めません ({ref.status_code})")
    made = gh("POST", "/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": ref.json()["object"]["sha"]})
    if made.status_code not in (201, 422):  # 422 = 同時に作られた
        raise StoreError(f"保存用ブランチを作れません ({made.status_code})")
    branch_ready["ok"] = True


def read_file(path):
    """(text or None, sha or None)"""
    ensure_branch()
    r = gh("GET", f"/contents/{path}", params={"ref": BRANCH})
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise StoreError(f"読み込みに失敗しました ({r.status_code})")
    j = r.json()
    if j.get("content"):
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
    # 1MB を超えるファイルは raw で取る
    raw = requests.get(j["download_url"], headers=HEADERS, timeout=20)
    raw.raise_for_status()
    return raw.text, j["sha"]


def write_file(path, text, sha, message):
    ensure_branch()
    body = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = gh("PUT", f"/contents/{path}", json=body)
    if r.status_code == 409 or r.status_code == 422:
        # 手作業の編集などで sha がずれた → 最新の sha で1回だけやり直す
        _, latest = read_file(path)
        if latest:
            body["sha"] = latest
        else:
            body.pop("sha", None)
        r = gh("PUT", f"/contents/{path}", json=body)
    if r.status_code not in (200, 201):
        raise StoreError(f"保存に失敗しました ({r.status_code})")
    return r.json()["content"]["sha"]


def delete_file(path, sha, message):
    ensure_branch()
    r = gh("DELETE", f"/contents/{path}", json={"message": message, "sha": sha, "branch": BRANCH})
    if r.status_code not in (200, 404):
        raise StoreError(f"削除に失敗しました ({r.status_code})")


def load_state():
    if cache["loaded"]:
        return
    text, sha = read_file(STATE_PATH)
    if text:
        j = json.loads(text)
        cache["rev"] = int(j.get("rev", 0))
        cache["state"] = j.get("state")
    cache["sha"] = sha
    cache["loaded"] = True


@app.errorhandler(StoreError)
def on_store_error(e):
    return jsonify({"error": str(e)}), 502


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.after_request
def no_cache(resp):
    # 端末やブラウザに古いデータ・古いページを残さない
    if request.path.startswith("/api/") or request.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
@app.get("/index.html")
def page():
    """抽選ページ本体。リポジトリの index.html をそのまま返す"""
    if os.path.exists(os.path.join(BASE_DIR, "index.html")):
        return send_from_directory(BASE_DIR, "index.html")
    return health()


@app.get("/prize<int:n>.<ext>")
def prize_file(n, ext):
    name = f"prize{n}.{ext}"
    if ext.lower() not in ("jpg", "jpeg", "png", "webp") or not os.path.exists(os.path.join(BASE_DIR, name)):
        return jsonify({"error": "写真がありません"}), 404
    return send_from_directory(BASE_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "repo": REPO, "branch": BRANCH, "token": bool(TOKEN)})


@app.get("/api/state")
def get_state():
    with lock:
        load_state()
        return jsonify({"rev": cache["rev"], "state": cache["state"]})


@app.post("/api/state")
def put_state():
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("prizes"), list):
        return jsonify({"error": "データの形式が正しくありません"}), 400
    base_rev = int(body.get("rev", -1))
    with lock:
        load_state()
        if base_rev != cache["rev"]:
            # 他の端末が先に保存した → 最新を返して、その端末に合わせてもらう
            return jsonify({"rev": cache["rev"], "state": cache["state"]}), 409
        new_rev = cache["rev"] + 1
        text = json.dumps({"rev": new_rev, "state": state}, ensure_ascii=False, separators=(",", ":"))
        cache["sha"] = write_file(STATE_PATH, text, cache["sha"], f"抽選データ更新 rev {new_rev}")
        cache["rev"] = new_rev
        cache["state"] = state
        return jsonify({"rev": new_rev})


def img_path(img_id):
    return f"data/img/{img_id}.txt"


@app.get("/api/img/<img_id>")
def get_img(img_id):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    with lock:
        if img_id not in img_cache:
            text, sha = read_file(img_path(img_id))
            if text is None:
                return jsonify({"error": "写真がありません"}), 404
            img_cache[img_id] = {"data": text, "sha": sha}
        return jsonify({"data": img_cache[img_id]["data"]})


@app.put("/api/img/<img_id>")
def put_img(img_id):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    data = (request.get_json(silent=True) or {}).get("data", "")
    if not isinstance(data, str) or not data.startswith("data:image/"):
        return jsonify({"error": "画像データではありません"}), 400
    with lock:
        sha = img_cache.get(img_id, {}).get("sha")
        if sha is None:
            _, sha = read_file(img_path(img_id))
        new_sha = write_file(img_path(img_id), data, sha, f"景品写真 {img_id}")
        img_cache[img_id] = {"data": data, "sha": new_sha}
        return jsonify({"ok": True})


@app.delete("/api/img/<img_id>")
def del_img(img_id):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    with lock:
        sha = img_cache.get(img_id, {}).get("sha")
        if sha is None:
            _, sha = read_file(img_path(img_id))
        if sha:
            delete_file(img_path(img_id), sha, f"景品写真を削除 {img_id}")
        img_cache.pop(img_id, None)
        return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
