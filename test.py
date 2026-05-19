import requests

WEBHOOK_URL = "https://discord.com/api/webhooks/1506193412963962901/dQL3cnUbrw8weGhZ_rcZu4iriE-RwVAFTDHpgkR4onlF9BRi8rz2DF33kwxXV2G8zUf_"

def send_youtube(
    youtube_url: str,
    title: str = "配信開始！",
    description: str = "",
    color: int = 0xff0000,
    username: str = "配信通知"
):
    """
    YouTube配信をEmbedで通知する関数
    
    Args:
        youtube_url (str): YouTubeのURL
        title (str): Embedのタイトル（クリックでリンク）
        description (str): 説明文（任意）
        color (int): Embedの色（0xで始まる16進数）
        username (str): Webhookの表示名
    """
    data = {
        "username": username,
        "content": "<@&1506186376599834634>",   # ロールメンション
        "embeds": [{
            "title": title,
            "url": youtube_url,                    # タイトルにリンク
            "color": color,
            "image": {
                "url": f"https://img.youtube.com/vi/{get_video_id(youtube_url)}/maxresdefault.jpg"
            }
        }]
    }
    
    response = requests.post(WEBHOOK_URL, json=data)
    
    if response.status_code == 204:
        print("✅ 送信成功！")
    else:
        print(f"❌ 送信失敗: {response.status_code}")
        print(response.text)


# YouTube URLから動画IDを自動取得する補助関数
def get_video_id(url: str) -> str:
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0]
    elif "watch?v=" in url:
        return url.split("watch?v=")[-1].split("&")[0]
    return url  # そのまま返す（念のため）


# ===================== 使用例 =====================

# 基本的な使い方
send_youtube(
    youtube_url="https://www.youtube.com/watch?v=V1xCHquP4pM",
    title="【ゼノブレイド2】配信開始！",
    color=0xff0000
)

# 他の例
# send_youtube(
#     youtube_url="https://www.youtube.com/watch?v=xxxxxxxxxxxxxxxx",
#     title="新規ゲーム配信！",
#     description="今日は〇〇やります",
#     color=0x00ff00,
#     username="ゲーム通知"
# )