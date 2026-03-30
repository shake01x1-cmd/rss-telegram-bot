import os
import re
import json
import time
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
STATE_PATH = Path("state.json")

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

SOURCE_LABELS = {
    "work24": "고용24",
    "saramin": "사람인",
    "jobkorea": "잡코리아",
}

MAX_PER_SOURCE_PER_KEYWORD = 2
MAX_MESSAGES_PER_RUN = 18
STATE_KEEP_DAYS = 14
REQUEST_TIMEOUT = 25

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }
)


def now_kst() -> datetime:
    return datetime.now(KST)


def jitter() -> None:
    time.sleep(random.uniform(2.2, 4.4))


def clean_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def unique_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_ids": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def prune_state(state: dict) -> None:
    keep_after = now_kst() - timedelta(days=STATE_KEEP_DAYS)
    pruned = {}
    for key, iso_value in state.get("seen_ids", {}).items():
        try:
            dt = datetime.fromisoformat(iso_value)
        except Exception:
            continue
        if dt >= keep_after:
            pruned[key] = iso_value
    state["seen_ids"] = pruned


def is_seen(state: dict, uid: str) -> bool:
    return uid in state.get("seen_ids", {})


def mark_seen(state: dict, uid: str) -> None:
    state.setdefault("seen_ids", {})[uid] = now_kst().isoformat()


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram secret is missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()


def fetch_html(url: str) -> str:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def parse_date_from_text(text: str):
    if not text:
        return None

    text = text.strip()

    patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일",
        r"(\d{2})/(\d{2})/(\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        parts = match.groups()
        if len(parts[0]) == 4:
            year, month, day = map(int, parts)
        else:
            year = 2000 + int(parts[0])
            month = int(parts[1])
            day = int(parts[2])

        try:
            return datetime(year, month, day, tzinfo=KST)
        except ValueError:
            return None

    return None


def extract_close_date_text(text: str) -> str:
    if not text:
        return "-"

    candidates = re.findall(r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", text)
    if len(candidates) >= 2:
        return candidates[1]
    if len(candidates) == 1:
        return candidates[0]

    k_candidates = re.findall(r"(20\d{2}년\s*\d{1,2}월\s*\d{1,2}일)", text)
    if len(k_candidates) >= 2:
        return k_candidates[1]
    if len(k_candidates) == 1:
        return k_candidates[0]

    m = re.search(r"(D-\d+|오늘마감|내일마감|상시채용|마감)", text)
    if m:
        return m.group(1)

    return "-"


def within_last_two_days(dt) -> bool:
    if dt is None:
        return False
    return dt >= now_kst() - timedelta(days=2)


def make_uid(job: dict) -> str:
    source = job.get("source", "")
    url = job.get("url", "")
    title = job.get("title", "")
    company = job.get("company", "")
    reg = job.get("reg_date_text", "")
    return f"{source}|{url}|{title}|{company}|{reg}"


def extract_candidates_by_href(soup: BeautifulSoup, href_keyword: str, base_url: str):
    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href_keyword not in href:
            continue

        url = urljoin(base_url, href)
        if url in seen_urls:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if len(title) < 4:
            continue

        node = a
        block_text = ""
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            block_text = clean_text(node.get_text("\n", strip=True))
            if title in block_text and len(block_text) >= len(title):
                break

        results.append(
            {
                "title": title,
                "url": url,
                "block_text": block_text,
            }
        )
        seen_urls.add(url)

    return results


def pick_company_from_lines(lines, title):
    bad_words = [
        "등록일", "수정일", "마감일", "근무지", "경력", "학력", "급여", "지원",
        "스크랩", "관심기업", "기업정보", "상세요강", "채용공고", "모집요강",
        "기간", "방법", "즉시지원", "파견", "정규직", "계약직", "아르바이트",
        "공고", "채용", "상세", "모집",
    ]
    title_idx = -1
    for i, line in enumerate(lines):
        if title and title in line:
            title_idx = i
            break

    search_order = []
    if title_idx != -1:
        for offset in [1, -1, 2, -2, 3, -3]:
            idx = title_idx + offset
            if 0 <= idx < len(lines):
                search_order.append(lines[idx])
    else:
        search_order = lines[:8]

    for line in search_order:
        if not line:
            continue
        if line == title:
            continue
        if any(word in line for word in bad_words):
            continue
        if len(line) > 50:
            continue
        return line

    return "-"


def format_date(dt) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d")


# -------------------------
# 고용24
# -------------------------
def search_work24(keyword: str):
    base_url = "https://m.work24.go.kr"
    url = (
        "https://m.work24.go.kr/wk/a/b/1200/retriveDtlEmpSrchList.do"
        f"?searchMode=Y&currentPageNo=1&pageIndex=1&sortField=DATE&sortOrderBy=DESC"
        f"&resultCnt=10&siteClcd=all&srcKeyword={quote_plus(keyword)}"
    )

    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text(" ", strip=True))
        if len(title) < 6:
            continue

        block_text = ""
        node = a
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            txt = clean_text(node.get_text("\n", strip=True))
            if "등록일" in txt and "마감일" in txt and title in txt:
                block_text = txt
                break

        if not block_text:
            continue

        url_abs = urljoin(base_url, a["href"])
        uid = f"{title}|{url_abs}"
        if uid in seen:
            continue
        seen.add(uid)

        lines = unique_keep_order(
            [clean_text(x) for x in block_text.split("\n") if clean_text(x)]
        )

        company = pick_company_from_lines(lines, title)
        reg_dt = parse_date_from_text(block_text)

        jobs.append(
            {
                "source": "work24",
                "keyword": keyword,
                "title": title,
                "company": company,
                "region": "-",
                "reg_dt": reg_dt,
                "reg_date_text": format_date(reg_dt),
                "close_date_text": extract_close_date_text(block_text),
                "url": url_abs,
            }
        )

    return jobs


# -------------------------
# 사람인
# -------------------------
def search_saramin(keyword: str):
    base_url = "https://www.saramin.co.kr"
    url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={quote_plus(keyword)}&recruitPage=1"

    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    candidates = extract_candidates_by_href(soup, "/zf_user/jobs/relay/view", base_url)

    jobs = []
    for item in candidates[:6]:
        block_text = item.get("block_text", "")
        reg_dt = parse_date_from_text(block_text)

        jobs.append(
            {
                "source": "saramin",
                "keyword": keyword,
                "title": item["title"],
                "company": "-",
                "region": "-",
                "reg_dt": reg_dt,
                "reg_date_text": format_date(reg_dt),
                "close_date_text": extract_close_date_text(block_text),
                "url": item["url"],
            }
        )
    return jobs


def hydrate_saramin(job: dict) -> dict:
    html = fetch_html(job["url"])
    soup = BeautifulSoup(html, "html.parser")

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        job["title"] = clean_text(og_title["content"])

    company = "-"
    for selector in [
        ".company_name",
        ".corp_name",
        ".recruit_company",
        "a[href*='/company-info/view']",
    ]:
        el = soup.select_one(selector)
        if el:
            company = clean_text(el.get_text(" ", strip=True))
            if company:
                break

    body = clean_text(soup.get_text("\n", strip=True))
    if company == "-":
        lines = unique_keep_order([clean_text(x) for x in body.split("\n") if clean_text(x)])
        company = pick_company_from_lines(lines, job["title"])

    reg_dt = job.get("reg_dt") or parse_date_from_text(body)

    job["company"] = company or "-"
    job["reg_dt"] = reg_dt
    job["reg_date_text"] = format_date(reg_dt)
    if job.get("close_date_text", "-") == "-":
        job["close_date_text"] = extract_close_date_text(body)
    return job


# -------------------------
# 잡코리아
# -------------------------
def search_jobkorea(keyword: str):
    urls = [
        f"https://www.jobkorea.co.kr/Search/?stext={quote_plus(keyword)}",
        f"https://m.jobkorea.co.kr/Search/?stext={quote_plus(keyword)}",
    ]

    html = ""
    for url in urls:
        try:
            html = fetch_html(url)
            if "/Recruit/GI_Read/" in html:
                break
        except Exception:
            continue

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    base_url = "https://m.jobkorea.co.kr"
    candidates = extract_candidates_by_href(soup, "/Recruit/GI_Read/", base_url)

    jobs = []
    for item in candidates[:6]:
        block_text = item.get("block_text", "")
        reg_dt = parse_date_from_text(block_text)
        mobile_url = item["url"].replace("https://www.jobkorea.co.kr", "https://m.jobkorea.co.kr")

        jobs.append(
            {
                "source": "jobkorea",
                "keyword": keyword,
                "title": item["title"],
                "company": "-",
                "region": "-",
                "reg_dt": reg_dt,
                "reg_date_text": format_date(reg_dt),
                "close_date_text": extract_close_date_text(block_text),
                "url": mobile_url,
            }
        )
    return jobs


def hydrate_jobkorea(job: dict) -> dict:
    html = fetch_html(job["url"])
    soup = BeautifulSoup(html, "html.parser")
    body = clean_text(soup.get_text("\n", strip=True))
    lines = unique_keep_order([clean_text(x) for x in body.split("\n") if clean_text(x)])

    if "채용공고" in lines:
        try:
            idx = lines.index("채용공고")
            if idx + 1 < len(lines):
                job["company"] = lines[idx + 1]
            if idx + 2 < len(lines):
                job["title"] = lines[idx + 2]
        except Exception:
            pass

    if job.get("company", "-") == "-":
        job["company"] = pick_company_from_lines(lines, job["title"])

    if job.get("reg_dt") is None:
        reg_dt = parse_date_from_text(body)
        job["reg_dt"] = reg_dt
        job["reg_date_text"] = format_date(reg_dt)

    if job.get("close_date_text", "-") == "-":
        job["close_date_text"] = extract_close_date_text(body)

    return job


def format_group_message(keyword: str, source: str, jobs: list[dict]) -> str:
    emoji = EMOJI_MAP.get(keyword, "🔘")
    source_label = SOURCE_LABELS.get(source, source)

    lines = [f"{emoji} {keyword} | {source_label} 최신 공고", ""]

    for idx, job in enumerate(jobs, start=1):
        lines.extend(
            [
                f"{idx}. {job.get('title', '-')}",
                f"회사: {job.get('company', '-')}",
                f"등록일: {job.get('reg_date_text', '-')}",
                f"마감: {job.get('close_date_text', '-')}",
                f"링크: {job.get('url', '-')}",
                "",
            ]
        )

    return "\n".join(lines).strip()


def main():
    state = load_state()
    prune_state(state)

    sent_messages = 0
    errors = []

    searchers = {
        "work24": search_work24,
        "saramin": search_saramin,
        "jobkorea": search_jobkorea,
    }

    hydrators = {
        "saramin": hydrate_saramin,
        "jobkorea": hydrate_jobkorea,
    }

    for keyword in KEYWORDS:
        for source in ["work24", "saramin", "jobkorea"]:
            try:
                jobs = searchers[source](keyword)
                jitter()
            except Exception as e:
                errors.append(f"{SOURCE_LABELS[source]} | {keyword} | 검색실패: {e}")
                continue

            filtered = []
            filtered_uids = []

            for job in jobs:
                try:
                    if source in hydrators and len(filtered) < MAX_PER_SOURCE_PER_KEYWORD:
                        job = hydrators[source](job)
                        jitter()
                except Exception as e:
                    errors.append(f"{SOURCE_LABELS[source]} | {keyword} | 상세파싱실패: {e}")

                if not within_last_two_days(job.get("reg_dt")):
                    continue

                uid = make_uid(job)
                if is_seen(state, uid):
                    continue

                filtered.append(job)
                filtered_uids.append(uid)

                if len(filtered) >= MAX_PER_SOURCE_PER_KEYWORD:
                    break

            if filtered:
                try:
                    send_telegram(format_group_message(keyword, source, filtered))
                    for uid in filtered_uids:
                        mark_seen(state, uid)
                    sent_messages += 1
                except Exception as e:
                    errors.append(f"텔레그램 전송실패 | {SOURCE_LABELS[source]} | {keyword} | {e}")

            if sent_messages >= MAX_MESSAGES_PER_RUN:
                break

        if sent_messages >= MAX_MESSAGES_PER_RUN:
            break

    save_state(state)

    if sent_messages == 0:
        send_telegram("ℹ️ 최근 2일 이내 신규 공고가 없거나, 아직 파싱된 결과가 없습니다.")

    if errors:
        summary = "\n".join(errors[:10])
        send_telegram(f"⚠️ 일부 소스 오류 요약\n{summary}")


if __name__ == "__main__":
    main()
