####
# にじさんじグッズ終了前リマインド通知
####

import os
import sys
import argparse
import logging
import re
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

END_REMINDERS = [
    {"field": "notified_end_7d", "days": 7, "color": 0x1E90FF, "label": "終了7日前"},
    {"field": "notified_end_3d", "days": 3, "color": 0xFFA500, "label": "終了3日前"},
    {"field": "notified_end_1d", "days": 1, "color": 0xFF0000, "label": "終了1日前"},
]


def get_collection_goods(uri: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db = client["nijisanji"]
    return db["goods"]


def parse_end_date(end_date: str) -> datetime | None:
    if not end_date:
        return None

    pattern = r"(?:(\d{4})年)?\s*(\d{1,2})月(\d{1,2})日\([月火水木金土日]\)\s*(\d{1,2}):(\d{2})"
    match = re.search(pattern, end_date)
    if not match:
        return None

    year_text, month_text, day_text, hour_text, minute_text = match.groups()
    year = int(year_text) if year_text else datetime.now(JST).year
    month = int(month_text)
    day = int(day_text)
    hour = int(hour_text)
    minute = int(minute_text)

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None

    if not year_text and dt < datetime.now(JST) - timedelta(days=180):
        dt = dt.replace(year=year + 1)

    return dt


def build_embed(doc: dict, label: str, color: int, role_id: str | None) -> dict:
    fields = [
        {"name": "名前", "value": doc.get("name", "（名称不明）"), "inline": False},
        {"name": "価格", "value": doc.get("price", "なし"), "inline": True},
        {"name": "終了日", "value": doc.get("end_date", "不明"), "inline": True},
        {"name": "タグ", "value": ", ".join(doc.get("tags", [])) if doc.get("tags") else "なし", "inline": False},
    ]

    embed = {
        "title": f"【{label}】{doc.get('name', '（名称不明）')}",
        "url": doc.get("url", ""),
        "color": color,
        "fields": fields,
        "footer": {"text": f"checked at: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST') }"},
    }

    if doc.get("image_url"):
        embed["thumbnail"] = {"url": doc["image_url"]}

    return embed


def post_discord(webhook_url: str, doc: dict, label: str, color: int, role_id: str | None) -> None:
    payload = {
        "username": "Nijisanji Goods End Reminder",
        "content": f"<@&{role_id}>" if role_id else "",
        "embeds": [build_embed(doc, label, color, role_id)],
    }

    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", 5))
        logger.warning("Discord レート制限 (429)。%.1f 秒後にリトライ", retry_after)
        time.sleep(retry_after)
        resp = requests.post(webhook_url, json=payload, timeout=10)

    resp.raise_for_status()
    logger.info("Discord 終了前リマインド送信: %s (%s)", doc.get("name"), label)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="グッズ終了前リマインド通知スクリプト")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGODB_URI"), help="MongoDB 接続文字列")
    parser.add_argument("--webhook", default=os.getenv("DISCORD_GOODS_WEBHOOK"), help="Discord Webhook URL")
    parser.add_argument("--notify-role-id", default=os.getenv("DISCORD_NIJISANJI_GOODS_NOTIFY_ROLE_ID"), help="通知する Discord ロール ID")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    missing = [k for k, v in {
        "mongo-uri": args.mongo_uri,
        "webhook": args.webhook,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)

    collection = get_collection_goods(args.mongo_uri)
    if collection is None:
        logger.error("MongoDB コレクションの取得に失敗しました")
        sys.exit(1)

    cursor = collection.find({
        "end_date": {"$exists": True, "$ne": None},
        "$or": [
            {"notified_end_7d": {"$exists": False}},
            {"notified_end_7d": False},
            {"notified_end_3d": {"$exists": False}},
            {"notified_end_3d": False},
            {"notified_end_1d": {"$exists": False}},
            {"notified_end_1d": False},
        ],
    })

    now = datetime.now(JST)
    total_sent = 0
    for doc in cursor:
        end_date_str = doc.get("end_date")
        end_dt = parse_end_date(end_date_str)
        if not end_dt:
            logger.warning("終了日の解析に失敗しました: %s", end_date_str)
            continue

        delta = end_dt - now
        remaining_days = delta.total_seconds() / 86400
        if remaining_days <= 0:
            logger.info("終了済みまたは終了直前のためスキップ: %s", doc.get("name"))
            continue

        notify_info = None
        for item in END_REMINDERS:
            notified = doc.get(item["field"], False)
            if not notified and item["days"] <= remaining_days < item["days"] + 1:
                notify_info = item
                break

        if not notify_info:
            logger.info("通知対象期間に該当しません: %s (残り %.2f 日)", doc.get("name"), remaining_days)
            continue

        try:
            post_discord(args.webhook, doc, notify_info["label"], notify_info["color"], args.notify_role_id)
            collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    notify_info["field"]: True,
                    "last_end_reminder_at": now.isoformat(),
                }},
            )
            total_sent += 1
        except requests.RequestException as exc:
            logger.error("Discord 送信失敗: %s (%s)", doc.get("name"), exc)

    logger.info("終了前リマインド送信完了: %d 件", total_sent)


if __name__ == "__main__":
    main()
