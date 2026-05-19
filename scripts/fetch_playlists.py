"""
fetch_playlists.py
==================
チャンネルの「ユーザー作成再生リスト」を YouTube Data API で取得し、
MongoDB に保存 → Discord に通知する。

動作モード（自動判定）:
    初回 : DBにデータが1件もない → 全件取得・保存・通知
    通常 : DBと差分比較 → 新規再生リストのみ通知

YouTube API エンドポイント:
    playlists.list (channelId 指定) でチャンネルの公開再生リストを取得。
    ページネーションで全件取得する。

MongoDB コレクション: youtube_notifications.playlists

環境変数:
    YOUTUBE_API_KEY   : YouTube Data API v3 のキー
    CHANNEL_ID        : 対象チャンネル ID (UC〇〇 形式)
    MONGODB_URI       : MongoDB 接続文字列
    DISCORD_WEBHOOK   : Discord Webhook URL
    ROLE_ID           : Discord ロール ID
"""

import os
import sys
import argparse
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
YOUTUBE_PLAYLISTS_URL  = "https://www.googleapis.com/youtube/v3/playlists"
YOUTUBE_PLAYLIST_PAGE  = "https://www.youtube.com/playlist?list="

DISCORD_COLOR_INITIAL  = 0x3498DB   # 青  : 初回一括通知
DISCORD_COLOR_NEW      = 0xFF0000   # 赤  : 新規再生リスト

# Discord Webhook レート制限対策
# 公式制限: 同一 Webhook で 30件/分 = 約2秒に1件
DISCORD_SEND_INTERVAL  = 2.0        # 通常の送信間隔（秒）
DISCORD_MAX_RETRIES    = 5          # 429 時の最大リトライ回数


# ══════════════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════════════

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

def fetch_all_playlists(api_key: str, channel_id: str) -> list[dict]:
    """
    チャンネルの公開再生リストを全件取得する（ページネーション対応）。
    戻り値: YouTube API の playlist リソースのリスト
    """
    results    = []
    next_token = None

    while True:
        params = {
            "part":       "snippet,contentDetails",
            "channelId":  channel_id,
            "maxResults": 50,
            "key":        api_key,
        }
        if next_token:
            params["pageToken"] = next_token

        resp = requests.get(YOUTUBE_PLAYLISTS_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results.extend(data.get("items", []))

        next_token = data.get("nextPageToken")
        if not next_token:
            break

    logger.info("YouTube から再生リストを %d 件取得", len(results))
    return results


def build_playlist_doc(item: dict) -> dict:
    """YouTube API レスポンスの item から MongoDB 保存用ドキュメントを生成する。"""
    snippet         = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    playlist_id     = item["id"]

    return {
        "playlistId":   playlist_id,
        "channelId":    snippet.get("channelId", ""),
        "title":        snippet.get("title", ""),
        "description":  snippet.get("description", ""),
        "thumbnailUrl": (snippet.get("thumbnails", {}).get("high", {}) or {}).get("url"),
        "videoCount":   content_details.get("itemCount", 0),
        "publishedAt":  to_jst_str(snippet.get("publishedAt")),
        "publishedRaw": snippet.get("publishedAt"),
        "notified":     False,
        "fetchedAt":    datetime.now(JST).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════

def get_collection(uri: str):
    """playlists コレクションを返す。"""
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    return client["youtube_notifications"]["playlists"]


def is_first_run(collection) -> bool:
    """DBにドキュメントが1件もなければ True（初回実行と判定）。"""
    return collection.count_documents({}) == 0


def get_existing_playlist_ids(collection) -> set[str]:
    """DB に保存済みの playlistId 一覧を返す。"""
    return {doc["playlistId"] for doc in collection.find({}, {"playlistId": 1})}


def upsert_playlist(collection, doc: dict) -> bool:
    """upsert。戻り値: True = 新規 / False = 既存"""
    result = collection.update_one(
        {"playlistId": doc["playlistId"]},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return result.upserted_id is not None


def mark_notified(collection, playlist_id: str) -> None:
    collection.update_one(
        {"playlistId": playlist_id},
        {"$set": {
            "notified":   True,
            "notifiedAt": datetime.now(JST).isoformat(),
        }},
    )


# ══════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════

def build_embed(doc: dict, color: int, label: str, roleID: str | None) -> dict:
    """Discord Embed を生成する。"""
    fields = [
        {"name": "動画数",     "value": str(doc.get("videoCount", 0)), "inline": True},
        {"name": "チャンネル", "value": doc.get("channelId", ""),       "inline": True},
    ]
    if doc.get("publishedAt"):
        fields.append({
            "name":   "作成日時",
            "value":  doc["publishedAt"],
            "inline": False,
        })
    if doc.get("description"):
        desc = doc["description"][:100] + ("…" if len(doc["description"]) > 100 else "")
        fields.append({
            "name":   "説明",
            "value":  desc,
            "inline": False,
        })

    embed = {
        "title":  doc.get("title", "（タイトル不明）"),
        "url":    YOUTUBE_PLAYLIST_PAGE + doc["playlistId"],
        "color":  color,
        "fields": fields,
        "image":  {"url": doc.get("thumbnailUrl")},
        "footer": {"text": f"playlistId: {doc['playlistId']} | fetchedAt: {doc.get('fetchedAt', '')}"},
    }
    if doc.get("thumbnailUrl"):
        embed["thumbnail"] = {"url": doc["thumbnailUrl"]}

    return embed


def post_discord(
    webhook_url: str,
    doc: dict,
    color: int,
    label: str,
    roleID: str | None,
) -> None:
    """
    Discord Webhook に再生リスト通知を送信する。

    レート制限対策:
      - 429 (Too Many Requests) が返った場合は Retry-After ヘッダの秒数だけ
        待機してリトライ（最大 DISCORD_MAX_RETRIES 回）
      - 送信成功後は呼び出し元で DISCORD_SEND_INTERVAL 秒のスリープを行う
    """
    payload = {
        "username": "YouTube Playlist Notifier",
        "content":  f"<@&{roleID}>" if roleID else "",
        "embeds":   [build_embed(doc, color, label, roleID)],
    }

    for attempt in range(1, DISCORD_MAX_RETRIES + 1):
        resp = requests.post(webhook_url, json=payload, timeout=10)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            logger.warning(
                "Discord レート制限 (429)。%.1f 秒後にリトライ [%d/%d]: %s",
                retry_after, attempt, DISCORD_MAX_RETRIES, doc["playlistId"],
            )
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        logger.info("Discord 通知送信: %s [%s]", doc["playlistId"], doc["title"][:30])
        return

    raise requests.RequestException(
        f"Discord への送信が {DISCORD_MAX_RETRIES} 回失敗しました: {doc['playlistId']}"
    )


def post_summary(webhook_url: str, total: int, channel_id: str) -> None:
    """初回実行時のサマリー通知を送信する。"""
    payload = {
        "username": "YouTube Playlist Notifier",
        "embeds": [{
            "title":       "📋 再生リスト 初回取得完了",
            "description": f"チャンネル `{channel_id}` の再生リストを **{total}件** 取得・保存しました。",
            "color":       DISCORD_COLOR_INITIAL,
            "footer":      {"text": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")},
        }],
    }
    requests.post(webhook_url, json=payload, timeout=10)


# ══════════════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="YouTube 再生リスト → MongoDB → Discord 通知")
    parser.add_argument("--api-key",    default=os.getenv("YOUTUBE_API_KEY"), help="YouTube Data API キー")
    parser.add_argument("--channel-id", default=os.getenv("CHANNEL_ID"),      help="YouTube チャンネル ID (UC〇〇)")
    parser.add_argument("--mongo-uri",  default=os.getenv("MONGODB_URI"),      help="MongoDB 接続文字列")
    parser.add_argument("--webhook",    default=os.getenv("DISCORD_WEBHOOK"),  help="Discord Webhook URL")
    parser.add_argument("--role-id",    default=os.getenv("ROLE_ID"),          help="Discord ロール ID")
    return parser.parse_args()


def main():
    args = parse_args()

    missing = [k for k, v in {
        "api-key":    args.api_key,
        "channel-id": args.channel_id,
        "mongo-uri":  args.mongo_uri,
        "webhook":    args.webhook,
        "role-id":    args.role_id,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)

    collection = get_collection(args.mongo_uri)
    first_run  = is_first_run(collection)

    if first_run:
        logger.info("=== 初回実行: 全再生リストを取得して保存・通知します ===")
    else:
        logger.info("=== 通常実行: 新規再生リストを差分チェックします ===")

    # ── YouTube API から全再生リストを取得 ──
    try:
        yt_items = fetch_all_playlists(args.api_key, args.channel_id)
    except requests.RequestException as e:
        logger.error("YouTube API エラー: %s", e)
        sys.exit(1)

    if not yt_items:
        logger.info("再生リストが見つかりませんでした。終了します。")
        return

    # ── 既存IDをDBから取得（通常実行時の差分検出用）──
    existing_ids = set() if first_run else get_existing_playlist_ids(collection)

    # ── 各再生リストを処理 ──
    new_count = 0
    for item in yt_items:
        doc         = build_playlist_doc(item)
        playlist_id = doc["playlistId"]

        upsert_playlist(collection, doc)

        if first_run:
            # 初回: 全件を青色で通知
            try:
                post_discord(
                    args.webhook, doc,
                    color=DISCORD_COLOR_INITIAL, label="初回登録",
                    roleID=args.role_id,
                )
                mark_notified(collection, playlist_id)
                new_count += 1
            except requests.RequestException as e:
                logger.error("Discord 通知失敗 (%s): %s", playlist_id, e)
            finally:
                # 成功・失敗問わず次の送信まで待機（レート制限対策）
                time.sleep(DISCORD_SEND_INTERVAL)

        else:
            # 通常: 新規のみ赤色で通知
            if playlist_id not in existing_ids:
                logger.info("[新規再生リスト] %s - %s", playlist_id, doc["title"][:40])
                try:
                    post_discord(
                        args.webhook, doc,
                        color=DISCORD_COLOR_NEW, label="新規再生リスト",
                        roleID=args.role_id,
                    )
                    mark_notified(collection, playlist_id)
                    new_count += 1
                except requests.RequestException as e:
                    logger.error("Discord 通知失敗 (%s): %s", playlist_id, e)
                finally:
                    time.sleep(DISCORD_SEND_INTERVAL)
            else:
                logger.info("[既存] %s をスキップ", playlist_id)

    # 初回実行時はサマリーも送信
    if first_run:
        try:
            post_summary(args.webhook, new_count, args.channel_id)
        except requests.RequestException as e:
            logger.error("サマリー通知失敗: %s", e)

    logger.info(
        "処理完了 (%s): 通知 %d 件 / 合計 %d 件",
        "初回" if first_run else "通常",
        new_count,
        len(yt_items),
    )


if __name__ == "__main__":
    main()
