"""
fetch_and_notify.py
====================
YouTube Data API から指定チャンネルの最新動画を取得し、
MongoDB に保存 → Discord に通知する。

環境変数:
    YOUTUBE_API_KEY   : YouTube Data API v3 のキー
    CHANNEL_ID        : 対象チャンネル ID
    MONGODB_URI       : MongoDB 接続文字列
    DISCORD_WEBHOOK   : Discord Webhook URL
"""

import os
import sys
import argparse
import re
import logging
from datetime import datetime, timezone, timedelta

import requests
from pymongo import MongoClient, errors as pymongo_errors

# ── ロガー設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ── 定数 ────────────────────────────────────────────────────
YOUTUBE_SEARCH_URL  = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL  = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_WATCH_URL   = "https://www.youtube.com/watch?v="

DISCORD_COLOR_NEW   = 0xFF0000  # 赤: 新着

# Shorts 判定のしきい値（秒）
SHORTS_MAX_DURATION = 60


# ══════════════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════════════

def parse_iso8601_duration(duration: str) -> int:
    """ISO 8601 duration (PT#M#S) を秒数に変換する。"""
    pattern = re.compile(
        r"P(?:(\d+)D)?"
        r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?"
    )
    m = pattern.fullmatch(duration)
    if not m:
        return 0
    days    = int(m.group(1) or 0)
    hours   = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def to_jst_str(iso_str: str | None) -> str | None:
    """UTC ISO 8601 文字列を JST の読みやすい文字列に変換する。"""
    if not iso_str:
        return None
    try:
        dt_utc = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_jst = dt_utc.astimezone(JST)
        return dt_jst.strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return iso_str


# ══════════════════════════════════════════════════════════════
# YouTube API
# ══════════════════════════════════════════════════════════════

def fetch_latest_video_ids(api_key: str, channel_id: str, max_results: int = 10) -> list[str]:
    """チャンネルの最新動画 ID 一覧を取得する。"""
    params = {
        "part":       "id",
        "channelId":  channel_id,
        "maxResults": max_results,
        "order":      "date",
        "type":       "video",
        "key":        api_key,
    }
    resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [item["id"]["videoId"] for item in items]


def fetch_video_details(api_key: str, video_ids: list[str]) -> list[dict]:
    """動画 ID のリストから詳細情報を取得する。"""
    if not video_ids:
        return []
    params = {
        "part":  "snippet,contentDetails,liveStreamingDetails",
        "id":    ",".join(video_ids),
        "key":   api_key,
    }
    resp = requests.get(YOUTUBE_VIDEOS_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("items", [])


def classify_video(item: dict) -> dict:
    """
    動画アイテムを解析し、MongoDB 保存用ドキュメントを返す。

    type フィールド:
        "live"    : ライブ配信中 / 配信予定
        "short"   : YouTube Shorts (60 秒以内)
        "video"   : 通常動画
    """
    snippet              = item.get("snippet", {})
    content_details      = item.get("contentDetails", {})
    live_details         = item.get("liveStreamingDetails", {})

    video_id   = item["id"]
    channel_id = snippet.get("channelId", "")
    title      = snippet.get("title", "")
    live_status = snippet.get("liveBroadcastContent", "none")  # "live" | "upcoming" | "none"

    duration_sec = parse_iso8601_duration(content_details.get("duration", "PT0S"))

    # タイプ判定
    if live_status in ("live", "upcoming"):
        video_type = "live"
    elif duration_sec <= SHORTS_MAX_DURATION and duration_sec > 0:
        video_type = "short"
    else:
        video_type = "video"

    # ライブの配信予定時刻 (JST)
    scheduled_start_jst = None
    scheduled_start_raw = None
    if video_type == "live":
        raw = live_details.get("scheduledStartTime") or live_details.get("actualStartTime")
        scheduled_start_raw = raw
        scheduled_start_jst = to_jst_str(raw)

    doc = {
        "videoId":            video_id,
        "channelId":          channel_id,
        "title":              title,
        "type":               video_type,          # "live" | "short" | "video"
        "liveStatus":         live_status,          # "live" | "upcoming" | "none"
        "scheduledStartJST":  scheduled_start_jst,
        "scheduledStartRaw":  scheduled_start_raw,
        "durationSec":        duration_sec,
        "thumbnailUrl":       (snippet.get("thumbnails", {}).get("high", {}) or {}).get("url"),
        "notified":           False,
        "fetchedAt":          datetime.now(JST).isoformat(),
    }
    return doc


# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════

def get_collection(uri: str):
    """MongoDB コレクションを返す。"""
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db     = client["youtube_notifications"]
    return db["videos"]


def upsert_video(collection, doc: dict) -> bool:
    """
    動画ドキュメントを upsert する。
    戻り値: True = 新規挿入 / False = 既存更新
    """
    result = collection.update_one(
        {"videoId": doc["videoId"]},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return result.upserted_id is not None


# ══════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════

def build_embed(doc: dict, color: int) -> dict:
    """Discord Embed オブジェクトを生成する。"""
    type_label = {
        "live":  "🔴 ライブ配信",
        "short": "⚡ Shorts",
        "video": "🎬 動画",
    }.get(doc["type"], doc["type"])

    fields = [
        {"name": "種別",      "value": type_label,     "inline": True},
        {"name": "チャンネル", "value": doc["channelId"], "inline": True},
    ]
    if doc.get("scheduledStartJST"):
        fields.append({
            "name":   "配信予定時刻",
            "value":  doc["scheduledStartJST"],
            "inline": False,
        })

    embed = {
        "title":       doc.get("title", "（タイトル不明）"),
        "url":         YOUTUBE_WATCH_URL + doc["videoId"],
        "color":       color,
        "fields":      fields,
        "footer":      {"text": f"fetchedAt: {doc.get('fetchedAt', '')}"},
    }
    if doc.get("thumbnailUrl"):
        embed["thumbnail"] = {"url": doc["thumbnailUrl"]}

    return embed


def post_discord(webhook_url: str, doc: dict, color: int = DISCORD_COLOR_NEW) -> None:
    """Discord Webhook に通知を送信する。"""
    payload = {
        "username": "YouTube Notifier",
        "embeds":   [build_embed(doc, color)],
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    logger.info("Discord 通知送信: %s", doc["videoId"])


# ══════════════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube → MongoDB → Discord 通知スクリプト")
    parser.add_argument("--api-key",    default=os.getenv("YOUTUBE_API_KEY"),  help="YouTube Data API キー")
    parser.add_argument("--channel-id", default=os.getenv("CHANNEL_ID"),       help="YouTube チャンネル ID")
    parser.add_argument("--mongo-uri",  default=os.getenv("MONGODB_URI"),       help="MongoDB 接続文字列")
    parser.add_argument("--webhook",    default=os.getenv("DISCORD_WEBHOOK"),   help="Discord Webhook URL")
    parser.add_argument("--max-results", type=int, default=10,                  help="取得する最大動画数 (default: 10)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── 必須パラメータチェック ──
    missing = [k for k, v in {
        "api-key":    args.api_key,
        "channel-id": args.channel_id,
        "mongo-uri":  args.mongo_uri,
        "webhook":    args.webhook,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)

    # ── YouTube → 動画 ID 取得 ──
    logger.info("チャンネル %s の最新動画を取得中...", args.channel_id)
    video_ids = fetch_latest_video_ids(args.api_key, args.channel_id, args.max_results)
    logger.info("取得した動画 ID 数: %d", len(video_ids))

    if not video_ids:
        logger.info("動画が見つかりませんでした。終了します。")
        return

    # ── 動画詳細取得 ──
    items = fetch_video_details(args.api_key, video_ids)
    logger.info("詳細取得完了: %d 件", len(items))

    # ── MongoDB 接続 ──
    collection = get_collection(args.mongo_uri)

    # ── 各動画を処理 ──
    new_count = 0
    for item in items:
        doc = classify_video(item)
        is_new = upsert_video(collection, doc)

        if is_new:
            new_count += 1
            logger.info("[新規] %s (%s) type=%s", doc["videoId"], doc["title"][:30], doc["type"])
            try:
                post_discord(args.webhook, doc, color=DISCORD_COLOR_NEW)
                # 通知済みフラグを更新
                collection.update_one(
                    {"videoId": doc["videoId"]},
                    {"$set": {"notified": True, "notifiedAt": datetime.now(JST).isoformat()}},
                )
            except requests.RequestException as e:
                logger.error("Discord 通知失敗 (%s): %s", doc["videoId"], e)
        else:
            logger.info("[既存] %s をスキップ", doc["videoId"])

    logger.info("処理完了: 新規 %d 件 / 合計 %d 件", new_count, len(items))


if __name__ == "__main__":
    main()