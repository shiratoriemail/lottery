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
import time

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPO", "shiratoriemail/lottery")
BRANCH = os.environ.get("DATA_BRANCH", "lottery-data")
# 入力ミス（前後の空白・末尾の / ・改行）があっても通るように整える
ORIGINS = [o.strip().rstrip("/") for o in re.split(r"[,\s]+", os.environ.get("ALLOWED_ORIGINS", "*")) if o.strip()] or ["*"]

API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NS_RE = re.compile(r"^[a-z0-9_-]{1,20}$")
# 別のページ（例: 商品名表示版 /api/ns/shohin/...）は data/<ns>/ に分けて保存する。ns なしは従来どおり data/


def ns_dir():
    ns = (request.view_args or {}).get("ns", "")
    if ns and not NS_RE.match(ns):
        raise StoreError("不正な保存先です")
    return f"data/{ns}/" if ns else "data/"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
CORS(app, origins=ORIGINS)

lock = threading.Lock()
caches = {}  # 保存先ごと: {"loaded","rev","state","sha"}
img_cache = {}  # path -> {"data": str, "sha": str}
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


def load_state(d):
    cache = caches.setdefault(d, {"loaded": False, "rev": 0, "state": None, "sha": None})
    if cache["loaded"]:
        return cache
    text, sha = read_file(d + "state.json")
    if text:
        j = json.loads(text)
        cache["rev"] = int(j.get("rev", 0))
        cache["state"] = j.get("state")
    cache["sha"] = sha
    cache["loaded"] = True
    return cache


@app.errorhandler(StoreError)
def on_store_error(e):
    return jsonify({"error": str(e)}), 502


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.after_request
def no_cache(resp):
    # 端末やブラウザに古いデータ・古いページを残さない
    if request.path.startswith("/api/") or request.path in ("/", "/index.html", "/shohin", "/shohin.html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
@app.get("/index.html")
def page():
    """抽選ページ本体。リポジトリの index.html をそのまま返す"""
    if os.path.exists(os.path.join(BASE_DIR, "index.html")):
        return send_from_directory(BASE_DIR, "index.html")
    return health()


@app.get("/shohin")
@app.get("/shohin.html")
def shohin_page():
    """商品名表示版のページ"""
    if os.path.exists(os.path.join(BASE_DIR, "shohin.html")):
        return send_from_directory(BASE_DIR, "shohin.html")
    return jsonify({"error": "shohin.html がリポジトリにありません"}), 404


@app.get("/prize<int:n>.<ext>")
def prize_file(n, ext):
    name = f"prize{n}.{ext}"
    if ext.lower() not in ("jpg", "jpeg", "png", "webp") or not os.path.exists(os.path.join(BASE_DIR, name)):
        return jsonify({"error": "写真がありません"}), 404
    return send_from_directory(BASE_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "repo": REPO, "branch": BRANCH, "token": bool(TOKEN), "ns": True, "remote": True})


@app.get("/api/state")
@app.get("/api/ns/<ns>/state")
def get_state(ns=""):
    d = ns_dir()
    with lock:
        cache = load_state(d)
        return jsonify({"rev": cache["rev"], "state": cache["state"]})


@app.post("/api/state")
@app.post("/api/ns/<ns>/state")
def put_state(ns=""):
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("prizes"), list):
        return jsonify({"error": "データの形式が正しくありません"}), 400
    base_rev = int(body.get("rev", -1))
    d = ns_dir()
    with lock:
        cache = load_state(d)
        if base_rev != cache["rev"]:
            # 他の端末が先に保存した → 最新を返して、その端末に合わせてもらう
            return jsonify({"rev": cache["rev"], "state": cache["state"]}), 409
        new_rev = cache["rev"] + 1
        text = json.dumps({"rev": new_rev, "state": state}, ensure_ascii=False, separators=(",", ":"))
        cache["sha"] = write_file(d + "state.json", text, cache["sha"], f"抽選データ更新 rev {new_rev}")
        cache["rev"] = new_rev
        cache["state"] = state
        return jsonify({"rev": new_rev})


def img_path(img_id):
    return f"{ns_dir()}img/{img_id}.txt"


@app.get("/api/img/<img_id>")
@app.get("/api/ns/<ns>/img/<img_id>")
def get_img(img_id, ns=""):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    with lock:
        if img_path(img_id) not in img_cache:
            text, sha = read_file(img_path(img_id))
            if text is None:
                return jsonify({"error": "写真がありません"}), 404
            img_cache[img_path(img_id)] = {"data": text, "sha": sha}
        return jsonify({"data": img_cache[img_path(img_id)]["data"]})


@app.put("/api/img/<img_id>")
@app.put("/api/ns/<ns>/img/<img_id>")
def put_img(img_id, ns=""):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    data = (request.get_json(silent=True) or {}).get("data", "")
    if not isinstance(data, str) or not data.startswith("data:image/"):
        return jsonify({"error": "画像データではありません"}), 400
    with lock:
        sha = img_cache.get(img_path(img_id), {}).get("sha")
        if sha is None:
            _, sha = read_file(img_path(img_id))
        new_sha = write_file(img_path(img_id), data, sha, f"景品写真 {img_id}")
        img_cache[img_path(img_id)] = {"data": data, "sha": new_sha}
        return jsonify({"ok": True})


@app.delete("/api/img/<img_id>")
@app.delete("/api/ns/<ns>/img/<img_id>")
def del_img(img_id, ns=""):
    if not ID_RE.match(img_id):
        return jsonify({"error": "不正なID"}), 400
    with lock:
        sha = img_cache.get(img_path(img_id), {}).get("sha")
        if sha is None:
            _, sha = read_file(img_path(img_id))
        if sha:
            delete_file(img_path(img_id), sha, f"景品写真を削除 {img_id}")
        img_cache.pop(img_path(img_id), None)
        return jsonify({"ok": True})


# ---------- 2台運用（映す画面＋リモコン） ----------
# リモコンの操作命令と、映す画面の状態をメモリに置く（保存はしない。再起動で消えてよい）
ACTIONS = {"reset", "draw", "next", "prev", "redo", "grandClose", "replay", "standby", "summary",
           "board", "photo", "extra", "start", "sound"}
remote_lock = threading.Lock()
remotes = {}  # 保存先ごと: {"seq", "cmds", "screens": {画面ID: {"status","ts","claim"}}}
ALIVE_SEC = 8  # この秒数以内に連絡のあった映す画面を「動いている」とみなす


def remote_of(d):
    return remotes.setdefault(d, {"seq": 0, "cmds": [], "screens": {}})


def leader_of(r, now):
    """動いている映す画面のうち、最後に「映す画面として開始」を押した1台だけを本物とする"""
    alive = {k: v for k, v in r["screens"].items() if now - v["ts"] < ALIVE_SEC}
    if not alive:
        return None, alive
    lead = max(alive, key=lambda k: (alive[k]["claim"], alive[k]["ts"]))
    return lead, alive


@app.post("/api/cmd")
@app.post("/api/ns/<ns>/cmd")
def post_cmd(ns=""):
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ACTIONS:
        return jsonify({"error": "不明な操作です"}), 400
    d = ns_dir()
    with remote_lock:
        r = remote_of(d)
        r["seq"] += 1
        r["cmds"].append({"seq": r["seq"], "action": action, "arg": body.get("arg"), "ts": time.time()})
        r["cmds"] = r["cmds"][-50:]
        return jsonify({"seq": r["seq"]})


@app.post("/api/screen")
@app.post("/api/ns/<ns>/screen")
def post_screen(ns=""):
    """映す画面が状態を送り、まだ実行していない命令を受け取る。
    映す画面が2台以上開いていても、命令を実行するのは1台（leader）だけ"""
    body = request.get_json(silent=True) or {}
    after = int(body.get("after", -1))
    sid = str(body.get("id") or "old")[:40]
    claim = float(body.get("claim") or 0)
    d = ns_dir()
    with remote_lock:
        r = remote_of(d)
        now = time.time()
        r["screens"][sid] = {"status": body.get("status"), "ts": now, "claim": claim}
        for k in [k for k, v in r["screens"].items() if now - v["ts"] > 600]:
            del r["screens"][k]
        lead, alive = leader_of(r, now)
        is_lead = lead == sid
        # after<0 は映す画面を開いた直後：それまでの命令は実行しない
        fresh = [c for c in r["cmds"] if c["seq"] > after] if (after >= 0 and is_lead) else []
        return jsonify({"seq": r["seq"], "cmds": fresh, "now": now, "leader": is_lead, "screens": len(alive)})


@app.get("/api/screen")
@app.get("/api/ns/<ns>/screen")
def get_screen(ns=""):
    d = ns_dir()
    with remote_lock:
        r = remote_of(d)
        now = time.time()
        lead, alive = leader_of(r, now)
        if lead is None:  # 動いている画面が無い：最後に連絡のあった画面を返す
            if not r["screens"]:
                return jsonify({"seq": r["seq"], "status": None, "age": None, "screens": 0})
            lead = max(r["screens"], key=lambda k: r["screens"][k]["ts"])
        v = r["screens"][lead]
        return jsonify({"seq": r["seq"], "status": v["status"], "age": now - v["ts"], "screens": len(alive)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
