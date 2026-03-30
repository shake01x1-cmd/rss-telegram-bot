import os
import requests
import time
import random
from datetime import datetime, timedelta

# ===== 설정 =====
KEYWORDS = [
    "AI아티스트",
    "AI영상",
    "영상제작",
    "영상편집",
    "라이브커머스",
    "Midjourney",
    "ComfyUI",
    "AI 콘텐츠 크리에이터",
    "색보정",
]

EMOJI_MAP = {
    "AI아티스트": "🟣",
    "AI영상": "🔵",
    "영상제작": "🟢",
    "영상편집": "🟡",
    "라이브커머스": "🟠",
    "Midjourney": "🔴",
    "ComfyUI": "⚫",
    "AI 콘텐츠 크리에이터": "🟤",
    "색보정": "⚪",
}


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    requests.post(url, data={
        "chat_id": chat_id,
        "text": text,
    })


# ===== 사람인 크롤링 =====
def crawl_saramin(keyword):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    url = f"https://www.saramin.co.kr/zf_user/search?searchword={keyword}"

    res = requests.get(url, headers=headers)
    html = res.text

    # 아주 간단 테스트용 파싱 (실전은 나중에 강화)
    results = []

    if "공고" in html:
        results.append(f"{keyword} 관련 공고 발견")

    time.sleep(random.uniform(2, 4))
    return results


# ===== 메인 =====
def main():
    now = datetime.now()
    cutoff = now - timedelta(days=2)

    for keyword in KEYWORDS:
        emoji = EMOJI_MAP.get(keyword, "🔘")

        jobs = crawl_saramin(keyword)

        for job in jobs:
            message = f"{emoji} [{keyword}]\n{job}"
            send_telegram(message)


if __name__ == "__main__":
    main()
