"""
remind_live.py
==============
MongoDB に保存済みの「配信予定ライブ」を YouTube Data API で再確認し、
まだ通知していないものを Discord に色を変えて再通知する。

環境変数:
    YOUTUBE_API_KEY   : YouTube Data API v3 のキー
    MONGODB_URI       : MongoDB 接続文字列
    DISCORD_WEBHOOK   : Discord Webhook URL
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta
import time

import requests
from pymongo import MongoClient

# ── ロガー設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ── 定数 ────────────────────────────────────────────────────
YOUTUBE_VIDEOS_URL   = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_WATCH_URL    = "https://www.youtube.com/watch?v="

# Discord Embed の色
DISCORD_COLOR_UPCOMING = 0xFFA500   # オレンジ: 配信予定（未通知）
DISCORD_COLOR_LIVE     = 0x00FF00   # 緑:       配信中

# リマインド対象: 配信開始〇分前から通知する範囲（分）
REMIND_BEFORE_MINUTES  = 30


# ══════════════════════════════════════════════════════════════
# YouTube API
# ══════════════════════════════════════════════════════════════

def fetch_video_details(api_key: str, video_ids: list[str]) -> list[dict]:
    """動画 ID のリストから詳細情報を取得する。"""
    if not video_ids:
        return []
    params = {
        "part": "snippet,liveStreamingDetails",
        "id":   ",".join(video_ids),
        "key":  api_key,
    }
    resp = requests.get(YOUTUBE_VIDEOS_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("items", [])


def get_live_status(item: dict) -> tuple[str, str | None]:
    """
    (liveStatus, scheduledStartJST) を返す。
    liveStatus: "live" | "upcoming" | "none"
    """
    snippet     = item.get("snippet", {})
    live_details = item.get("liveStreamingDetails", {})

    live_status = snippet.get("liveBroadcastContent", "none")

    raw = live_details.get("scheduledStartTime") or live_details.get("actualStartTime")
    scheduled_jst = None
    if raw:
        try:
            dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            dt_jst = dt_utc.astimezone(JST)
            scheduled_jst = dt_jst.strftime("%Y-%m-%d %H:%M:%S JST")
        except Exception:
            scheduled_jst = raw

    return live_status, scheduled_jst


# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════

def get_collection(uri: str):
    """MongoDB コレクションを返す。"""
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db     = client["youtube_notifications"]
    return db["videos"]


def fetch_unnotified_lives(collection) -> list[dict]:
    """
    type == "live" かつ notified == False のドキュメントを返す。
    """
    cursor = collection.find({
        "type":     "live",
        "notified": False,
    })
    return list(cursor)


def is_within_remind_window(scheduled_jst_str: str | None) -> bool:
    """
    scheduledStartJST が現在時刻から REMIND_BEFORE_MINUTES 分以内であれば True。
    scheduled_jst_str が None の場合（すでに配信中など）も True を返す。
    """
    if not scheduled_jst_str:
        return True
    try:
        # "2024-01-01 12:00:00 JST" → datetime
        dt = datetime.strptime(scheduled_jst_str, "%Y-%m-%d %H:%M:%S JST")
        dt = dt.replace(tzinfo=JST)
        now = datetime.now(JST)
        diff = (dt - now).total_seconds() / 60  # 分
        # 配信開始 REMIND_BEFORE_MINUTES 分前〜配信後60分以内を対象
        return -60 <= diff <= REMIND_BEFORE_MINUTES
    except Exception:
        return True


# ══════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════

def build_embed(doc: dict, live_status: str, color: int, roleID: str | None) -> dict:
    """Discord Embed オブジェクトを生成する。"""
    status_label = {
        "live":     "🟢 配信中！",
        "upcoming": "🟠 まもなく配信予定",
    }.get(live_status, "🔴 ライブ")

    fields = [
        {"name": "ステータス",  "value": status_label,        "inline": True},
        {"name": "チャンネル",  "value": doc.get("channelId", ""), "inline": True},
    ]
    if doc.get("scheduledStartJST"):
        fields.append({
            "name":   "配信予定時刻",
            "value":  doc["scheduledStartJST"],
            "inline": False,
        })

    embed = {
        "title":  f"【リマインド】{doc.get('title', '（タイトル不明）')}",
        "content": f"{f'<@&{roleID}>' if roleID else ''}",  # ロールメンション（あれば）
        "url":    YOUTUBE_WATCH_URL + doc["videoId"],
        "color":  color,
        "fields": fields,
        "image": {
                "url": f"https://img.youtube.com/vi/{doc['videoId']}/maxresdefault.jpg"
        },
        "footer": {"text": f"remind checked at: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}"},
    }
    if doc.get("thumbnailUrl"):
        embed["thumbnail"] = {"url": doc["thumbnailUrl"]}

    return embed


def post_discord(webhook_url: str, doc: dict, live_status: str, color: int, roleID: str | None) -> None:
    """Discord Webhook に通知を送信する。"""
    payload = {
        "username": "YouTube Live Reminder",
        "content": f"{f'<@&{roleID}>' if roleID else ''}",  # ロールメンション（あれば）
        "embeds":   [build_embed(doc, live_status, color, roleID)],
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    
    if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            logger.warning(
                "Discord レート制限 (429)。%.1f 秒後にリトライ",
                retry_after,
            )
            time.sleep(retry_after)
            resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    logger.info("Discord リマインド送信: %s", doc["videoId"])


# ══════════════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ライブ配信リマインド通知スクリプト")
    parser.add_argument("--api-key",   default=os.getenv("YOUTUBE_API_KEY"), help="YouTube Data API キー")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGODB_URI"),     help="MongoDB 接続文字列")
    parser.add_argument("--webhook",   default=os.getenv("DISCORD_WEBHOOK"), help="Discord Webhook URL")
    parser.add_argument("--role-id",   default=os.getenv("ROLE_ID"), help="Discord ロール ID")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── 必須パラメータチェック ──
    missing = [k for k, v in {
        "api-key":   args.api_key,
        "mongo-uri": args.mongo_uri,
        "webhook":   args.webhook,
        "role-id":   args.role_id,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)

    # ── MongoDB から未通知ライブを取得 ──
    collection = get_collection(args.mongo_uri)
    unnotified = fetch_unnotified_lives(collection)
    logger.info("未通知ライブ件数: %d", len(unnotified))

    if not unnotified:
        logger.info("リマインド対象なし。終了します。")
        return

    # ── YouTube API で最新ステータスを確認 ──
    video_ids = [doc["videoId"] for doc in unnotified]
    items      = fetch_video_details(args.api_key, video_ids)
    item_map   = {item["id"]: item for item in items}

    remind_count = 0
    for doc in unnotified:
        vid   = doc["videoId"]
        item  = item_map.get(vid)

        if not item:
            logger.warning("YouTube から動画情報を取得できませんでした: %s", vid)
            continue

        live_status, scheduled_jst = get_live_status(item)

        # 配信終了済み（none）→ DB を更新してスキップ
        if live_status == "none":
            collection.update_one(
                {"videoId": vid},
                {"$set": {
                    "liveStatus": "none",
                    "notified":   True,
                    "notifiedAt": datetime.now(JST).isoformat(),
                    "note":       "配信終了 or 非公開化によりスキップ",
                }},
            )
            logger.info("[スキップ] 配信終了/削除: %s", vid)
            continue

        # scheduledStartJST を最新値で更新
        if scheduled_jst and scheduled_jst != doc.get("scheduledStartJST"):
            collection.update_one(
                {"videoId": vid},
                {"$set": {"scheduledStartJST": scheduled_jst}},
            )
            doc["scheduledStartJST"] = scheduled_jst

        # リマインドウィンドウ内かチェック
        if not is_within_remind_window(doc.get("scheduledStartJST")):
            logger.info("[スキップ] リマインド時間外: %s (予定: %s)", vid, doc.get("scheduledStartJST"))
            continue

        # 色を決定
        color = DISCORD_COLOR_LIVE if live_status == "live" else DISCORD_COLOR_UPCOMING

        try:
            post_discord(args.webhook, doc, live_status, color, args.role_id if hasattr(args, "role_id") else None)
            remind_count += 1

            # 通知済みに更新
            collection.update_one(
                {"videoId": vid},
                {"$set": {
                    "notified":      True,
                    "notifiedAt":    datetime.now(JST).isoformat(),
                    "liveStatus":    live_status,
                    "remindedColor": hex(color),
                }},
            )
        except requests.RequestException as e:
            logger.error("Discord 送信失敗 (%s): %s", vid, e)

    logger.info("リマインド完了: %d 件送信", remind_count)


if __name__ == "__main__":
    main()