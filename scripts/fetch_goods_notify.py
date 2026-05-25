####
#にじさんじグッズの通知
####

import os
import sys
import argparse
import re
import logging
import time
import enum
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
import requests
from pymongo import MongoClient

# ── ENUM ──────────────────────────────────────────────
class SaleStatus(enum.Enum):
    ON_SALE = "販売中"
    END_OF_SALE = "販売終了"
    COMING_SOON = "まもなく販売"
    RESELL = "再販"

# ── ロガー設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ── 定数 ────────────────────────────────────────────────────
BASE_URL = "https://shop.nijisanji.jp"
GOODS_URL = "https://shop.nijisanji.jp/category?pageNo=5&prefn1=endOfSale&prefv1=%E8%B2%A9%E5%A3%B2%E4%B8%AD&start=100&sz=12"


# ══════════════════════════════════════════════════════════════
# MongoDB
# ══════════════════════════════════════════════════════════════
def get_collection_goods(uri: str):
    """MongoDB コレクションを返す。"""
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db     = client["nijisanji"]
    return db["goods"]


def parse_args():
    parser = argparse.ArgumentParser(description="YouTube → MongoDB → Discord 通知スクリプト")
    parser.add_argument("--mongo-uri",   default=os.getenv("MONGODB_URI"),      help="MongoDB 接続文字列")
    parser.add_argument("--webhook",     default=os.getenv("DISCORD_GOODS_WEBHOOK"),  help="Discord Webhook URL")
    parser.add_argument("--notify_role_id", default=os.getenv("DISCORD_NIJISANJI_GOODS_NOTIFY_ROLE_ID"), help="通知する Discord ロール ID")
    return parser.parse_args()


def main():
    logger.info("Starting fetch_goods_notify.py") 
    
    args = parse_args()
    
    # argsのチェック
    missing = [k for k, v in {
        "mongo-uri":  args.mongo_uri,
        "webhook":    args.webhook,
        "notify_role_id": args.notify_role_id,
    }.items() if not v]
    if missing:
        logger.error("必須パラメータが未設定です: %s", ", ".join(missing))
        sys.exit(1)
        
    collection_goods = get_collection_goods(args.mongo_uri)
    
    if collection_goods is None:
        logger.error("MongoDB コレクションの取得に失敗しました")
        sys.exit(1)
        
    
    # Playwright を使用してグッズ情報を取得
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        logger.info("グッズページにアクセス: %s", GOODS_URL)
        page.goto(GOODS_URL, timeout=30000)
        
        page.wait_for_load_state("networkidle", timeout=30000)
        # 商品情報を抽出
        # classのgrid-colを持つ要素を全て取得
        items = page.query_selector_all(".grid-col")
        logger.info("グッズ情報を抽出中...")
        #ここあらまずdict形式で情報を取得する
        goods_list = []
        for item in items:
            try:
                
                #classのSSZS-73873.htmlのhrefからURLを取得する
                # <a href="/products/SSZS-73873.html">
                url_elem = item.query_selector("a")
                if not url_elem:
                    continue
                url = url_elem.get_attribute("href")
                
                #classのcard-thumbの下にあるimgや、titleから情報を取得する
                # <img src="/on/demandware.static/-/Sites-nijisanji-master-catalog/default/dw7a3f7ef3/physical/SSZS-73873_0.png" alt="リゼ・ヘルエスタ誕生日グッズ＆ボイス2026" title="リゼ・ヘルエスタ誕生日グッズ＆ボイス2026" width="280" height="280">
                img = item.query_selector(".card-thumb img")
                if not img:
                    continue
                img_src = img.get_attribute("src")
                img_alt = img.get_attribute("alt")
                img_title = img.get_attribute("title")

                #classのlabel-listに下にあるliからタグを取得　NEW,再販,まもなく終了,まもなく販売のタグがある
                #enumのSaleStatusに対応させる
                tags = []
                label_list = item.query_selector(".label-list")
                if label_list:
                    for li in label_list.query_selector_all("li"):
                        text = li.inner_text().strip()
                        if text == "NEW":
                            tags.append(SaleStatus.ON_SALE.value)
                        elif text == "再販":
                            tags.append(SaleStatus.RESELL.value)
                        elif text == "まもなく終了":
                            tags.append(SaleStatus.END_OF_SALE.value)
                        elif text == "まもなく販売":
                            tags.append(SaleStatus.COMING_SOON.value)
                #classのtext-priceから価格を取得
                price_elem = item.query_selector(".text-price")
                price = price_elem.inner_text().strip() if price_elem else None

                #事前に取得したmongoDBのデータと比較して、新しいグッズがあれば通知する
                existing = collection_goods.find_one({"name": img_title})
                if existing:
                    logger.info("既存のグッズ: %s", img_title)
                    continue
                
                #dictに追加
                goods_list.append({
                    "name": img_title,
                    "url": BASE_URL + url,
                    "image_url": BASE_URL + img_src,
                    "tags": tags,
                    "price": price,
                })
            
            except Exception as e:
                logger.error("グッズ情報の抽出に失敗: %s", e)

        logger.info("新しいグッズの数: %d", len(goods_list))
        #グッズごとにページにアクセスして、発売日を取得する
            
        for goods in goods_list:
            try:
                page.goto(goods["url"], timeout=30000)
                page.wait_for_load_state("networkidle", timeout=30000)
                
                #classのlink-list link-list-liverのliから、ライバー名と、ライバーのURLを取得する
                #ここは複数のライバーがいる場合もあるので、リストで取得する
                liver_list = []
                link_list = page.query_selector(".link-list.link-list-liver")
                if link_list:
                    for li in link_list.query_selector_all("li"):
                        a = li.query_selector("a")
                        if a:
                            liver_name = a.inner_text().strip()
                            liver_url = a.get_attribute("href")
                            liver_list.append({
                                "name": liver_name,
                                "url": BASE_URL + liver_url
                            })
                
                #tag-list swiper-wrapperのliから複数のタグを取得する タグ名と、タグのURLを取得する
                tag_list = []
                tag_list_elem = page.query_selector(".tag-list.swiper-wrapper")
                if tag_list_elem:
                    for li in tag_list_elem.query_selector_all("li"):
                        a = li.query_selector("a")
                        if a:
                            tag_name = a.inner_text().strip()
                            tag_url = a.get_attribute("href")
                            tag_list.append({
                                "name": tag_name,
                                "url": BASE_URL + tag_url
                            })
                #accordion-body js-accordion-body product-description-blockの中から、発売日と説明を取得する
                #発売日は　2026年5月25日(月)18:00 ～ 5月31日(日)23:59のような形式で記載されているので、正規表現で抽出する
                release_date = None
                description = None 
                accordion_body = page.query_selector(".accordion-body.js-accordion-body.product-description-block")
                if accordion_body:
                    text = accordion_body.inner_text().strip()
                    #発売日の正規表現
                    # (\d{4}年)? で「年」があってもなくてもOKにする
                    pattern = r"((?:\d{4}年)?\d{1,2}月\d{1,2}日\([月火水木金土日]\)\d{1,2}:\d{2})\s*～\s*((?:\d{4}年)?\d{1,2}月\d{1,2}日\([月火水木金土日]\)\d{1,2}:\d{2})"
                    match = re.search(pattern, text)
                    if match:
                        release_date = match.group(1)
                        end_date = match.group(2)
                        #end_dateに年がない場合は、release_dateの年をend_dateに追加する
                        if "年" not in end_date and "年" in release_date:
                            year = re.search(r"(\d{4})年", release_date).group(1)
                            end_date = f"{year}年{end_date}"
                        goods["release_date"] = release_date
                        goods["end_date"] = end_date
                    description = text  
                created_at = datetime.now(JST)
                
                #MongoDBに保存
                collection_goods.insert_one({
                    "name": goods["name"],
                    "url": goods["url"],
                    "image_url": goods["image_url"],
                    "tags": goods["tags"],
                    "price": goods["price"],
                    "livers": liver_list,
                    "categories": tag_list,
                    "release_date": release_date,
                    "end_date": end_date,
                    "description": description,
                    "created_at": created_at
                })
                
                #Discordに通知
                #WebhookのURLは引数で渡す
                webhook_url = args.webhook
                #通知する内容は、グッズの名前、URL、画像、価格、発売日、説明、タグ、ライバー名とURLを含める
                fields = [
                    {"name": "名前", "value": goods["name"], "inline": False},
                    {"name": "価格", "value": goods["price"], "inline": True},
                    {"name": "発売日", "value": release_date or "不明", "inline": True},
                    {"name": "終了日", "value": end_date or "不明", "inline": True},
                    {"name": "タグ", "value": ", ".join(goods["tags"]) if goods["tags"] else "なし", "inline": False},
                    {"name": "ライバー", "value": "\n".join([f"[{l['name']}]({l['url']})" for l in liver_list]) if liver_list else "なし", "inline": False},
                    {"name": "説明", "value": description or "なし", "inline": False},
                ]
                embed = {
                    "content": f"<@&{args.notify_role_id}>",
                    "title": goods["name"],
                    "url": goods["url"],
                    "color": 0xFFFFFF,
                    "thumbnail": {"url": goods["image_url"]},
                    "fields": fields,
                }
                
                payload = {
                            "username": "Nijisanji Goods Bot",
                            "embeds":   [embed],
                }
                res = requests.post(args.webhook, json=payload, timeout=10)
                if res.status_code == 429:
                    logger.error(f"Failed to send webhook message for talent {goods['name']}: {res.status_code} Too Many Requests")
                    retry_after = float(res.headers.get("Retry-After", 5))
                    logger.warning(
                                "Discord レート制限 (429)。%.1f ",
                                retry_after,
                    )
                    time.sleep(retry_after)
                    res = requests.post(args.webhook, json=payload, timeout=10)
                    
                
                        
                
            except Exception as e:
                print(f"Error occurred while processing {goods['name']}: {e}")

            

if __name__ == "__main__":
    main()