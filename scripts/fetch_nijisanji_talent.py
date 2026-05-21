import argparse
from datetime import datetime, timedelta, timezone
import logging
import os
import sys
import time

from playwright.sync_api import sync_playwright
import enum

from pymongo import MongoClient
import requests


JST = timezone(timedelta(hours=9))

# ── ロガー設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

URL = "https://www.nijisanji.jp/talents?filter="

# ── タレントの種類 ─────────────────────────────────────────────
class TalentType(enum.Enum):
    NIJISANJI_JP = "nijisanji"
    NIJISANJI_EN = "nijisanjien"
    VIRTUALREAL  = "virtuareal"
    
# ── 汎用 ─────────────────────────────────────────────
def change_color_code(color: str | None) -> str | None:
    """CSSの色表現を「#RRGGBB」形式に変換する。"""
    if not color:
        return None

    try:
        rgb_list = [int(x.strip()) for x in color.split(",")]
        r, g, b = rgb_list
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception as e:
        logger.warning(f"Failed to parse color '{color}': {e}")
        return None

                
    return color  # すでに「#RRGGBB」形式の場合はそのまま返す

def change_color_code_int(color: str | None) -> int | None:
    """CSSの色表現を整数のRGB値に変換する。"""
    hex_color = change_color_code(color)
    if hex_color and hex_color.startswith("#") and len(hex_color) == 7:
        try:
            return int(hex_color[1:], 16)
        except ValueError as e:
            logger.warning(f"Failed to convert color '{hex_color}' to int: {e}")
            return None
    return None

def get_url_for_type(talent_type: TalentType) -> str:
    """タレントの種類に応じた URL を返す。"""
    if talent_type == TalentType.NIJISANJI_JP:
        return URL + "nijisanji"
    elif talent_type == TalentType.NIJISANJI_EN:
        return URL + "nijisanjien"
    elif talent_type == TalentType.VIRTUALREAL:
        return URL + "virtuareal"
    else:
        raise ValueError(f"Unknown talent type: {talent_type}") 

# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════
def get_collection_talent(uri: str):
    """MongoDB コレクションを返す。"""
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db     = client["nijisanji"]
    return db["talents"]


# ══════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════

def build_embed(talent_name : str,talent_img_url: str | None,talent_details_url: str | None, description: str | None, youtube_url : str | None, twitter_url : str | None,color: int) -> dict:


    fields = [
        {"name": "名前",       "value": talent_name,       "inline": False},
        {"name": "説明",   "value": description,       "inline": False},
        {"name": "YouTube",   "value": youtube_url,       "inline": True},
        {"name": "Twitter",   "value": twitter_url,       "inline": True},
    ]
    embed = {
        "title":  talent_name,
        "url":    talent_details_url,
        "color":  color,
        "fields": fields,
        "image":  {"url": talent_img_url} 
    }

    return embed



def parse_args():
    parser = argparse.ArgumentParser(description="YouTube → MongoDB → Discord 通知スクリプト")
    parser.add_argument("--mongo-uri",   default=os.getenv("MONGODB_URI"),      help="MongoDB 接続文字列")
    parser.add_argument("--webhook",     default=os.getenv("DISCORD_WEBHOOK"),  help="Discord Webhook URL")
    return parser.parse_args()

def main():
    logger.info("Starting talent fetching...")
    
    # argsを取得
    args = parse_args()
    
    # argsのチェック
    missing = [k for k, v in {
        "mongo-uri":  args.mongo_uri,
        "webhook":    args.webhook,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)
        
    # MongoDB コレクションの取得
    collection = get_collection_talent(args.mongo_uri)
    if collection is None:
        logger.error("MongoDB コレクションの取得に失敗しました。")
        sys.exit(1)
        
    # ブラウザの起動はループの外側で行う（高速化・省リソース）
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for talent_type in TalentType:
            url = get_url_for_type(talent_type)
            logger.info(f"Fetching talents from URL: {url}")
            
            try:
                page.goto(url)
                page.wait_for_load_state("networkidle")
                # ページを一番下までスクロールして全てのタレントを読み込む
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)  # 2秒待機
                page.wait_for_load_state("networkidle")
                
                talent_elements = page.query_selector_all('[data-testid="TalentItem"]')
                logger.info(f"Found {len(talent_elements)} talents for type {talent_type.value}")
                
                # --- 改善ポイント1: 最初に一覧のデータ（テキストや要素のインデックス）を抽出する ---
                talents_list = []
                for i, elem in enumerate(talent_elements):
                    img_elem = elem.query_selector("img")
                    img_url = img_elem.get_attribute("src") if img_elem else None
                    if(img_elem == None or img_url == None):
                        logger.warning(f"画像URLが見つかりませんでした。インデックス: {i}")
                        page.wait_for_load_state("networkidle")
                        img_elem = elem.query_selector("img")
                        img_url = img_elem.get_attribute("src") if img_elem else None
                    
                    name_elem = elem.query_selector("p")
                    name = name_elem.inner_text().strip() if name_elem else None
                    
                    if name:
                        talents_list.append({
                            "index": i,
                            "name": name,
                            "img_url": img_url
                        })
                
                # 抽出したリストを元に詳細ページへアクセス
                for t in talents_list:
                    name = t["name"]
                    img_url = "https://www.nijisanji.jp/" + t["img_url"] if t["img_url"] else None
                    
                    try:
                        # MongoDBで既存データをチェック
                        existing = collection.find_one({"name": name})
                        
                        # --- 改善ポイント2: 既存チェックの集約 ---
                        # 「全く未登録」か「登録済みだが画像URLなどの基本情報が変化している」場合のみ詳細ページにいく
                        # ※もし「常に詳細ページの情報（説明文など）の更新をチェックしたい」場合は、このif文を外してください
                        if existing and existing.get("img_url") == img_url:
                            logger.info(f"Talent already exists and no change in list page, skipping: {name}")
                            continue
                        
                        # 一覧ページに戻っていることを確認して、インデックスを元に再度ボタンを取得（要素の生存エラー対策）
                        current_elements = page.query_selector_all('[data-testid="TalentItem"]')
                        if t["index"] >= len(current_elements):
                            logger.warning(f"Index out of range for talent {name}, skipping.")
                            continue
                        
                        button_elem = current_elements[t["index"]].query_selector("button")
                        if not button_elem:
                            continue
                            
                        # ボタンをクリックして詳細ページへ移動
                        with page.expect_navigation():
                            button_elem.click()
                        
                        # 移動後のURLを取得
                        talent_url = page.url
                        page.wait_for_load_state("domcontentloaded")

                        # さらに、情報が詰まっている「説明文のクラス」が画面に現れるまでピンポイントで待つ
                        page.wait_for_selector('[class^="liver-profile_liverDescription__"]', timeout=5000)
                        
                        # --- 詳細ページからの情報取得 ---
                        # 色の取得
                        color_elem = page.query_selector('[class^="liver-profile_upperParts__"]')
                        color_style = color_elem.get_attribute("style") if color_elem else None
                        color = None
                        if color_style and "background: linear-gradient" in color_style:
                            try:
                                start = color_style.index("rgb(") + len("rgb(")
                                end = color_style.index(")", start)
                                color = color_style[start:end]
                            except ValueError:
                                logger.warning(f"色のパースに失敗しました: {color_style}")
                                
                        # 説明の取得
                        description_elem = page.query_selector('[class^="liver-profile_liverDescription__"]')
                        description = description_elem.inner_text().strip() if description_elem else None
                        
                        # SNS情報の取得
                        sns_elements = page.query_selector_all('[class^="sns-link_snsLink__"]')
                        youtube_url = None
                        youtube_channel_id = None
                        twitter_url = None
                        for sns_elem in sns_elements:
                            href = sns_elem.get_attribute("href")
                            if not href:
                                continue
                            if "youtube.com" in href:
                                youtube_url = href
                                if "channel/" in href:
                                    youtube_channel_id = href.split("channel/")[-1]
                            elif "twitter.com" in href or "x.com" in href:
                                twitter_url = href  
                        
                        # --- 改善ポイント3: データベースへの保存・更新ロジックの整理 ---
                        if existing:
                            # 変更があるかチェック
                            if (existing.get("img_url") != img_url or
                                existing.get("talent_url") != talent_url or
                                existing.get("color") != color or
                                existing.get("description") != description or
                                existing.get("youtube_url") != youtube_url or
                                existing.get("youtube_channel_id") != youtube_channel_id or
                                existing.get("twitter_url") != twitter_url  ):
                                
                                collection.update_one({"_id": existing["_id"]}, {"$set": {
                                    "img_url": img_url,
                                    "talent_url": talent_url,
                                    "color": color,
                                    "description": description,
                                    "youtube_url": youtube_url,
                                    "youtube_channel_id": youtube_channel_id,
                                    "twitter_url": twitter_url,
                                    "updated_at": datetime.now(JST),
                                }})
                                logger.info(f"Updated talent: {name}")
                            else:
                                logger.info(f"No changes for talent: {name}")
                        else:
                            # 新規追加
                            collection.insert_one({
                                "name": name,
                                "img_url": img_url,
                                "talent_url": talent_url,
                                "color": color,
                                "description": description,
                                "youtube_url": youtube_url,
                                "youtube_channel_id": youtube_channel_id,
                                "twitter_url": twitter_url,
                                "created_at": datetime.now(JST),
                            })
                            logger.info(f"Added new talent: {name}")
                            
                            #discordのbotに通知する処理
                            post = build_embed(name, img_url, talent_url, description=description, youtube_url=youtube_url, twitter_url=twitter_url, color=change_color_code_int(color))
                            payload = {
                                "username": "Nijisanji Talent Bot",
                                "embeds":   [post],
                            }
                            res = requests.post(args.webhook, json=payload,timeout=10)
                            if res.status_code == 429:
                                logger.error(f"Failed to send webhook message for talent {name}")
                                retry_after = float(res.headers.get("Retry-After", 5))
                                logger.warning(
                                    "Discord レート制限 (429)。%.1f ",
                                    retry_after,
                                )
                                time.sleep(retry_after)
                                res = requests.post(args.webhook, json=payload)
                        # 一覧ページに戻る
                        page.go_back()
                        page.wait_for_load_state("domcontentloaded")
                        page.wait_for_timeout(3000)  # 3秒待機

                    except Exception as detail_e:
                        logger.error(f"Error processing talent {name}: {detail_e}")
                        # エラーが起きても一覧ページに戻れるように保険をかけておく
                        if page.url != url:
                            page.goto(url)
                            page.wait_for_load_state("domcontentloaded")
                            page.wait_for_timeout(2000) # 2秒待機
                            
            except Exception as e:
                logger.error(f"Error processing talent_type {talent_type.value}: {e}")
                
        browser.close()
main()