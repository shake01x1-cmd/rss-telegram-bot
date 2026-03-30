import os
import requests
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9))

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

WORKNET_API_KEY = os.getenv("WORKNET_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram secret is missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def parse_worknet_date(value: str):
    if not value:
        return None

    value = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def text_of(node, tag_name: str) -> str:
    child = node.find(tag_name)
    return child.text.strip() if child is not None and child.text else ""


def fetch_worknet_jobs(keyword: str):
    if not WORKNET_API_KEY:
        raise ValueError("WORKNET_API_KEY is missing")

    url = "https://openapi.work.go.kr/opi/opi/opia/wantedApi.do"

    params = {
        "authKey": WORKNET_API_KEY,
        "returnType": "XML",
        "callTp": "L",
        "startPage": "1",
        "display": "10",
        "keyword": keyword,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    jobs = []

    for item in root.findall(".//wanted"):
        title = text_of(item, "title")
        company = text_of(item, "company")
        region = text_of(item, "region")
        close_date = text_of(item, "closeDt")
        regist_date = text_of(item, "regDt")
        detail_url = text_of(item, "wantedInfoUrl")

        reg_dt = parse_worknet_date(regist_date)

        jobs.append({
            "title": title,
            "company": company,
            "region": region,
            "close_date": close_date,
            "regist_date": regist_date,
            "reg_dt": reg_dt,
            "url": detail_url,
        })

    return jobs


def format_job_message(keyword: str, job: dict) -> str:
    emoji = EMOJI_MAP.get(keyword, "🔘")

    lines = [
        f"{emoji} {keyword}으로 검색된 최신 공고",
        f"제목: {job['title'] or '-'}",
        f"회사: {job['company'] or '-'}",
        f"지역: {job['region'] or '-'}",
        f"등록일: {job['regist_date'] or '-'}",
        f"마감일: {job['close_date'] or '-'}",
        f"링크: {job['url'] or '-'}",
    ]
    return "\n".join(lines)


def main():
    now = datetime.now(KST)
    cutoff = now - timedelta(days=2)

    total_sent = 0

    for keyword in KEYWORDS:
        try:
            jobs = fetch_worknet_jobs(keyword)
        except Exception as e:
            send_telegram(f"❌ 워크넷 조회 실패\n키워드: {keyword}\n오류: {e}")
            continue

        filtered = []
        for job in jobs:
            if job["reg_dt"] is None:
                continue
            if job["reg_dt"] < cutoff:
                continue
            filtered.append(job)

        for job in filtered[:3]:
            msg = format_job_message(keyword, job)
            send_telegram(msg)
            total_sent += 1

    if total_sent == 0:
        send_telegram("ℹ️ 최근 2일 이내 워크넷 신규 공고가 없습니다.")


if __name__ == "__main__":
    main()
