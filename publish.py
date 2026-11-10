"""Publish due posts from queue.json to Instagram. Runs in GitHub Actions every 15 min.

Env (GitHub secrets): IG_USER_ID, IG_ACCESS_TOKEN, optional GRAPH_API_VERSION.
queue.json entries (written by scripts/publish_prep.py on Marc's PC):
  {"id": "2026-12/p01", "format": "carousel|image|reel", "publish_at_utc": "...Z",
   "caption": "...", "image_urls": [...], "video_url": "...", "cover_url": "...",
   "status": "queued|published|failed", "attempts": 0, "container_id": null}
Rules: max 3 attempts per post, then status=failed and the workflow exits 1 (GitHub emails the owner).
Never double-posts: a container already PUBLISHED is detected before retrying.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
LOG = HERE / "published.log"
GRAPH = f"https://graph.facebook.com/{os.getenv('GRAPH_API_VERSION', 'v24.0')}"
IG = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
MAX_ATTEMPTS = 3


class PublishError(RuntimeError):
    pass


def api(method: str, path: str, **params) -> dict:
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}"
    r = (requests.get(url, params=params, timeout=90) if method == "GET"
         else requests.post(url, data=params, timeout=90))
    data = r.json() if r.content else {}
    if not r.ok or "error" in data:
        raise PublishError(f"{method} {path}: HTTP {r.status_code} {data.get('error', data)}")
    return data


def url_ok(url: str) -> bool:
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def wait_ready(container_id: str, timeout_s: int = 600) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = api("GET", container_id, fields="status_code,status").get("status_code")
        if st in ("FINISHED", "PUBLISHED"):
            return st
        if st in ("ERROR", "EXPIRED"):
            detail = api("GET", container_id, fields="status").get("status")
            raise PublishError(f"container {container_id} {st}: {detail}")
        time.sleep(10)
    raise PublishError(f"container {container_id} not ready after {timeout_s}s")


def create_container(post: dict) -> str:
    cap = post.get("caption", "")
    fmt = post["format"]
    if fmt == "image":
        return api("POST", f"{IG}/media", image_url=post["image_urls"][0], caption=cap)["id"]
    if fmt == "carousel":
        children = []
        for u in post["image_urls"]:
            cid = api("POST", f"{IG}/media", image_url=u, is_carousel_item="true")["id"]
            children.append(cid)
        for cid in children:
            wait_ready(cid, 300)
        return api("POST", f"{IG}/media", media_type="CAROUSEL", children=",".join(children), caption=cap)["id"]
    if fmt == "reel":
        params = {"media_type": "REELS", "video_url": post["video_url"], "caption": cap, "share_to_feed": "true"}
        if post.get("cover_url"):
            params["cover_url"] = post["cover_url"]
        return api("POST", f"{IG}/media", **params)["id"]
    raise PublishError(f"unknown format {fmt}")


def publish(post: dict) -> None:
    # Resume safely: if a previous run already created (or even published) this container
    cid = post.get("container_id")
    if cid:
        try:
            st = wait_ready(cid, 600)
        except PublishError:
            cid, st = None, None
        if st == "PUBLISHED":
            raise PublishError("ALREADY_PUBLISHED")  # handled by caller
    if not cid:
        urls = post.get("image_urls") or [post.get("video_url")]
        missing = [u for u in urls if not url_ok(u)]
        if missing:
            raise PublishError(f"media URL not reachable yet (GitHub Pages still deploying?): {missing[0]}")
        cid = create_container(post)
        post["container_id"] = cid
        save()  # persist container id before publishing
        wait_ready(cid, 900 if post["format"] == "reel" else 300)
    media_id = api("POST", f"{IG}/media_publish", creation_id=cid)["id"]
    post["media_id"] = media_id
    post["permalink"] = api("GET", media_id, fields="permalink").get("permalink")


_queue: list = []


def save() -> None:
    QUEUE.write_text(json.dumps(_queue, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    global _queue
    if not IG or not TOKEN:
        sys.exit("IG_USER_ID / IG_ACCESS_TOKEN secrets missing")
    _queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else []
    now = datetime.now(timezone.utc)
    due = [p for p in _queue if p.get("status") == "queued"
           and datetime.fromisoformat(p["publish_at_utc"].replace("Z", "+00:00")) <= now]
    print(f"{len(due)} post(s) due of {len(_queue)} in queue")
    newly_failed = []
    for post in due:
        post["attempts"] = post.get("attempts", 0) + 1
        try:
            publish(post)
            post["status"] = "published"
            post["published_at"] = now.isoformat()
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{now.isoformat()}\t{post['id']}\t{post['format']}\t{post.get('permalink')}\n")
            print(f"PUBLISHED {post['id']} -> {post.get('permalink')}")
        except PublishError as e:
            if str(e) == "ALREADY_PUBLISHED":
                post["status"] = "published"
                post["note"] = "container was already published (recovered)"
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"{now.isoformat()}\t{post['id']}\t{post['format']}\tLIVE (recovered; see profile)\n")
                print(f"RECOVERED {post['id']} (already live)")
            else:
                post["last_error"] = str(e)[:500]
                print(f"ERROR {post['id']} attempt {post['attempts']}: {e}")
                if post["attempts"] >= MAX_ATTEMPTS:
                    post["status"] = "failed"
                    newly_failed.append(post["id"])
                    with LOG.open("a", encoding="utf-8") as fh:
                        fh.write(f"{now.isoformat()}\t{post['id']}\t{post['format']}\tFAILED: {str(e)[:200]}\n")
        save()
    if newly_failed:
        sys.exit(f"FAILED after {MAX_ATTEMPTS} attempts: {newly_failed}")


if __name__ == "__main__":
    main()
