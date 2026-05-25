"""
fetch_and_notify.py
====================
YouTube Data API のプレイリストエンドポイントを使い、
種別ごと（ライブ / 動画 / Shorts）に正確に取得して
MongoDB に保存 → Discord に通知する。

チャンネルIDの UC〇〇 プレフィックスを以下に置換してプレイリストIDとして使用：
    UULV〇〇  : ライブ配信タブ
    UULF〇〇  : 動画タブ
    UUSH〇〇  : Shortsタブ

環境変数:
    YOUTUBE_API_KEY   : YouTube Data API v3 のキー
    MONGODB_URI       : MongoDB 接続文字列

データベース:
    nijisanji.talents に role_id, youtube_channel_id, webhook_url を保持していること
"""

import os
import sys
import argparse
import re
import logging
import time
from datetime import datetime, timezone, timedelta

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
YOUTUBE_PLAYLIST_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_VIDEOS_URL   = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_WATCH_URL    = "https://www.youtube.com/watch?v="

DISCORD_COLOR_NEW    = 0xFF0000  # 赤: 新着

# Discord Webhook レート制限対策
# 公式制限: 同一 Webhook で 30件/分 = 約2秒に1件
DISCORD_SEND_INTERVAL = 2.0      # 通常の送信間隔（秒）
DISCORD_MAX_RETRIES   = 5        # 429 時の最大リトライ回数


# ══════════════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════════════

def channel_id_to_playlist_id(channel_id: str, prefix: str) -> str:
    """
    UC〇〇 → {prefix}〇〇 に変換してプレイリストIDを返す。
    例: channel_id="UCabc", prefix="UULV" → "UULVabc"
    """
    if not channel_id.startswith("UC"):
        raise ValueError(f"チャンネルIDが UC で始まっていません: {channel_id}")
    return prefix + channel_id[2:]


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


def to_jst_str(iso_str):
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

def fetch_playlist_video_ids(api_key: str, playlist_id: str, max_results: int = 5) -> list:
    """
    プレイリスト ID から最新の動画 ID 一覧を取得する。
    プレイリストが存在しない場合は空リストを返す。
    """
    params = {
        "part":       "contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key":        api_key,
    }
    resp = requests.get(YOUTUBE_PLAYLIST_URL, params=params, timeout=10)

    if resp.status_code == 404:
        logger.warning("プレイリストが存在しません（スキップ）: %s", playlist_id)
        return []

    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [item["contentDetails"]["videoId"] for item in items]


def fetch_video_details(api_key: str, video_ids: list) -> list:
    """動画 ID のリストから詳細情報を取得する。"""
    if not video_ids:
        return []
    params = {
        "part": "snippet,contentDetails,liveStreamingDetails",
        "id":   ",".join(video_ids),
        "key":  api_key,
    }
    resp = requests.get(YOUTUBE_VIDEOS_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("items", [])


def is_free_chat(item: dict) -> bool:
    """
    フリーチャット枠を判定して除外する。
    ・タイトルにフリーチャット系ワードを含む
    ・scheduledStartTime も actualStartTime もなく配信終了済み
    """
    snippet      = item.get("snippet", {})
    live_details = item.get("liveStreamingDetails", {})
    title        = snippet.get("title", "").lower()

    free_chat_keywords = ["フリーチャット", "free chat", "freechat", "待機所", "待機枠"]
    if any(kw in title for kw in free_chat_keywords):
        return True

    has_schedule = bool(
        live_details.get("scheduledStartTime") or live_details.get("actualStartTime")
    )
    live_status = snippet.get("liveBroadcastContent", "none")

    if not has_schedule and live_status == "none":
        return True

    return False


def build_doc(item: dict, video_type: str) -> dict:
    """
    動画アイテムと確定済みの video_type から MongoDB 保存用ドキュメントを返す。
    video_type: "live" | "video" | "short"  ← プレイリストタブで確定済み
    """
    snippet      = item.get("snippet", {})
    live_details = item.get("liveStreamingDetails", {})

    video_id    = item["id"]
    channel_id  = snippet.get("channelId", "")
    title       = snippet.get("title", "")
    live_status = snippet.get("liveBroadcastContent", "none")

    scheduled_start_jst = None
    scheduled_start_raw = None
    if video_type == "live":
        raw = live_details.get("scheduledStartTime") or live_details.get("actualStartTime")
        scheduled_start_raw = raw
        scheduled_start_jst = to_jst_str(raw)

    duration_sec = parse_iso8601_duration(
        item.get("contentDetails", {}).get("duration", "PT0S")
    )

    return {
        "videoId":           video_id,
        "channelId":         channel_id,
        "title":             title,
        "type":              video_type,
        "liveStatus":        live_status,
        "scheduledStartJST": scheduled_start_jst,
        "scheduledStartRaw": scheduled_start_raw,
        "durationSec":       duration_sec,
        "thumbnailUrl":      (snippet.get("thumbnails", {}).get("high", {}) or {}).get("url"),
        "notified":          False,
        "fetchedAt":         datetime.now(JST).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════

def get_db(uri: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    return client


def get_collection(uri: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    return client["youtube_notifications"]["videos"]


def get_talents(client) -> list[dict]:
    db = client["nijisanji"]
    return list(db["talents"].find({}))


def is_valid_talent(talent: dict) -> bool:
    return bool(
        talent.get("youtube_channel_id") and
        talent.get("webhook_url") and
        talent.get("role_id")
    )


def upsert_video(collection, doc: dict) -> bool:
    """upsert。戻り値: True = 新規 / False = 既存"""
    result = collection.update_one(
        {"videoId": doc["videoId"]},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return result.upserted_id is not None


# ══════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════

def build_embed(doc: dict, color: int, roleID: str) -> dict:
    type_label = {
        "live":  "🔴 ライブ配信",
        "short": "⚡ Shorts",
        "video": "🎬 動画",
    }.get(doc["type"], doc["type"])

    fields = [
        {"name": "種別",       "value": type_label,       "inline": True},
        {"name": "チャンネル", "value": doc["channelId"], "inline": True},
    ]
    if doc.get("scheduledStartJST"):
        fields.append({
            "name":   "配信予定時刻",
            "value":  doc["scheduledStartJST"],
            "inline": False,
        })

    embed = {
        "title":  doc.get("title", "（タイトル不明）"),
        "url":    YOUTUBE_WATCH_URL + doc["videoId"],
        "color":  color,
        "fields": fields,
        "image":  {"url": f"https://img.youtube.com/vi/{doc['videoId']}/maxresdefault.jpg"},
        "footer": {"text": f"fetchedAt: {doc.get('fetchedAt', '')}"},
    }
    if doc.get("thumbnailUrl"):
        embed["thumbnail"] = {"url": doc["thumbnailUrl"]}

    return embed


def post_discord(
    webhook_url: str,
    doc: dict,
    color: int = DISCORD_COLOR_NEW,
    roleID: str = None,
) -> None:
    """
    Discord Webhook に動画通知を送信する。

    レート制限対策:
      - 429 (Too Many Requests) が返った場合は Retry-After ヘッダの秒数だけ
        待機してリトライ（最大 DISCORD_MAX_RETRIES 回）
      - 送信成功後は呼び出し元で DISCORD_SEND_INTERVAL 秒のスリープを行う
    """
    payload = {
        "username": "YouTube Notifier",
        "content":  f"<@&{roleID}>" if roleID else "",
        "embeds":   [build_embed(doc, color, roleID)],
    }

    for attempt in range(1, DISCORD_MAX_RETRIES + 1):
        resp = requests.post(webhook_url, json=payload, timeout=10)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            logger.warning(
                "Discord レート制限 (429)。%.1f 秒後にリトライ [%d/%d]: %s",
                retry_after, attempt, DISCORD_MAX_RETRIES, doc["videoId"],
            )
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        logger.info("Discord 通知送信: %s [%s]", doc["videoId"], doc["type"])
        return

    raise requests.RequestException(
        f"Discord への送信が {DISCORD_MAX_RETRIES} 回失敗しました: {doc['videoId']}"
    )


# ══════════════════════════════════════════════════════════════
# プレイリストごとの処理
# ══════════════════════════════════════════════════════════════

def process_playlist(
    api_key, channel_id, playlist_prefix, video_type,
    max_results, collection, webhook_url, roleID
) -> int:
    """1種別分のプレイリストを取得・保存・通知。戻り値: 新規通知件数"""
    playlist_id = channel_id_to_playlist_id(channel_id, playlist_prefix)
    logger.info("[%s] プレイリスト %s を取得中...", video_type, playlist_id)

    video_ids = fetch_playlist_video_ids(api_key, playlist_id, max_results)
    if not video_ids:
        logger.info("[%s] 動画なし", video_type)
        return 0

    items     = fetch_video_details(api_key, video_ids)
    new_count = 0

    for item in items:
        vid = item["id"]

        # フリーチャット除外（ライブタブのみ）
        if video_type == "live" and is_free_chat(item):
            logger.info("[%s] フリーチャットをスキップ: %s / %s",
                        video_type, vid, item.get("snippet", {}).get("title", "")[:30])
            continue

        doc    = build_doc(item, video_type)
        is_new = upsert_video(collection, doc)

        if is_new:
            new_count += 1
            logger.info("[新規/%s] %s - %s", video_type, vid, doc["title"][:40])
            try:
                post_discord(webhook_url, doc, color=DISCORD_COLOR_NEW, roleID=roleID)
                collection.update_one(
                    {"videoId": vid},
                    {"$set": {"notifiedAt": datetime.now(JST).isoformat()}},
                )
            except requests.RequestException as e:
                logger.error("Discord 通知失敗 (%s): %s", vid, e)
            finally:
                # 成功・失敗問わず次の送信まで待機（レート制限対策）
                time.sleep(DISCORD_SEND_INTERVAL)
        else:
            logger.info("[既存/%s] %s をスキップ", video_type, vid)

    return new_count


# ══════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="YouTube → MongoDB → Discord 通知スクリプト")
    parser.add_argument("--api-key",   default=os.getenv("YOUTUBE_API_KEY"), help="YouTube Data API キー")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGODB_URI"),      help="MongoDB 接続文字列")
    parser.add_argument("--max-results", type=int, default=5,                 help="各タブから取得する最大動画数 (default: 5)")
    return parser.parse_args()


def process_talent_videos(talent: dict, api_key: str, collection, max_results: int) -> int:
    role_id    = talent.get("role_id")
    webhook    = talent.get("webhook_url")
    channel_id = talent.get("youtube_channel_id")

    if not is_valid_talent(talent):
        logger.warning(
            "Talent %s をスキップします。必須フィールド不足: %s",
                talent.get("name"),
            {"id": talent.get("_id"), "channel_id": channel_id, "role_id": role_id, "webhook_url": webhook},
        )
        return 0

    targets = [
        ("UULV", "live"),   # ライブ配信タブ → type: live
        ("UULF", "video"),  # 動画タブ       → type: video
        ("UUSH", "short"),  # Shortsタブ     → type: short
    ]

    talent_count = 0
    for prefix, vtype in targets:
        try:
            n = process_playlist(
                api_key         = api_key,
                channel_id      = channel_id,
                playlist_prefix = prefix,
                video_type      = vtype,
                max_results     = max_results,
                collection      = collection,
                webhook_url     = webhook,
                roleID          = role_id,
            )
            talent_count += n
        except requests.RequestException as e:
            logger.error("[%s/%s] API エラー: %s", channel_id, vtype, e)
    return talent_count


def main():
    args = parse_args()

    missing = [k for k, v in {
        "api-key":   args.api_key,
        "mongo-uri": args.mongo_uri,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)

    client     = get_db(args.mongo_uri)
    talents    = get_talents(client)
    collection = get_collection(args.mongo_uri)

    if not talents:
        logger.info("talents が見つかりません。終了します。")
        return

    total_new = 0
    for talent in talents:
        total_new += process_talent_videos(talent, args.api_key, collection, args.max_results)

    logger.info("全talent処理完了: 新規通知 %d 件", total_new)


if __name__ == "__main__":
    main()