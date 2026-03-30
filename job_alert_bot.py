import os
import re
import json
import time
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
        "Upgrade-Insecure-Requests": "1",
    }
)

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=1.2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)


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


def fetch_html(url: str, referer: str | None = None) -> str:
    headers = {}
    if referer:
        headers["Referer"] = referer
    response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_lines_from_soup(soup: BeautifulSoup):
    return unique_keep_order(
        [clean_text(s) for s in soup.stripped_strings if clean_text(s)]
    )


def find_first_date_text(text: str):
    if not text:
        return None

    patterns = [
        r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
        r"(20\d{2}년\s*\d{1,2}월\s*\d{1,2}일)",
        r"(\d{2}/\d{2}/\d{2})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()

    return None


def parse_date_from_text(text: str):
    if not text:
        return None

    date_text = find_first_date_text(text)
    if not date_text:
        return None

    patterns = [
        ("%Y.%m.%d", r"\."),
        ("%Y-%m-%d", r"-"),
        ("%Y/%m/%d", r"/"),
    ]

    for fmt, sep in patterns:
        if re.search(sep, date_text):
            try:
                return datetime.strptime(date_text, fmt).replace(tzinfo=KST)
            except ValueError:
                pass

    m = re.search(r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일", date_text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
        except ValueError:
            return None

    try:
        return datetime.strptime(date_text, "%y/%m/%d").replace(tzinfo=KST)
    except ValueError:
        return None


def extract_labeled_date(lines, labels):
    compact_labels = [re.sub(r"\s+", "", x) for x in labels]

    for i, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)
        for label in compact_labels:
            if label in compact:
                dt = parse_date_from_text(line)
                if dt:
                    return dt, find_first_date_text(line) or "-"
                for j in range(i + 1, min(i + 3, len(lines))):
                    dt = parse_date_from_text(lines[j])
                    if dt:
                        return dt, find_first_date_text(lines[j]) or "-"
    return None, "-"


def extract_labeled_company(lines):
    labels = ["회사명", "기업명", "사업체명", "업체명", "기관명", "회사", "기업"]
    stop_words = ["등록일", "마감일", "근무", "학력", "경력", "급여", "근무지", "채용", "모집", "상세요강"]

    for i, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)
        for label in labels:
            label_compact = re.sub(r"\s+", "", label)
            if compact.startswith(label_compact):
                m = re.match(rf"^{re.escape(label)}\s*[:：]?\s*(.+)$", line)
                if m:
                    value = clean_text(m.group(1))
                    if value and not any(word in value for word in stop_words):
                        return value

                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = clean_text(lines[j])
                    if not candidate:
                        continue
                    if any(word in candidate for word in stop_words):
                        break
                    if len(candidate) <= 50:
                        return candidate
    return "-"


def extract_close_date_text(lines):
    _, close_text = extract_labeled_date(lines, ["마감일", "접수마감", "마감", "접수기간"])
    if close_text != "-":
        return close_text

    body = " ".join(lines)
    m = re.search(r"(D-\d+|오늘마감|내일마감|상시채용|채용시까지|마감)", body)
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


def clean_title(title: str) -> str:
    title = clean_text(title)
    bad_titles = {"본문 바로가기", "주메뉴 바로가기", "바로가기"}
    if title in bad_titles:
        return ""
    if "바로가기" in title:
        return ""
    return title


def extract_meta_title(soup: BeautifulSoup) -> str:
    for selector in [
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ]:
        el = soup.select_one(selector[0])
        if el and el.get(selector[1]):
            return clean_text(el.get(selector[1]))

    if soup.title and soup.title.text:
        return clean_text(soup.title.text)

    return ""


def extract_candidates_by_href(soup: BeautifulSoup, href_keyword: str, base_url: str):
    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href_keyword not in href:
            continue
        if href.startswith("javascript:"):
            continue
        if href.startswith("#"):
            continue

        url = urljoin(base_url, href)
        if url in seen_urls:
            continue

        title = clean_title(a.get_text(" ", strip=True))
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


def format_date(dt) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d")


# -------------------------
# 고용24
# -------------------------
def search_work24(keyword: str):
    urls = [
        (
            "https://m.work24.go.kr/wk/a/b/1200/retriveDtlEmpSrchList.do"
            f"?searchMode=Y&currentPageNo=1&pageIndex=1&sortField=DATE&sortOrderBy=DESC"
            f"&resultCnt=10&siteClcd=all&srcKeyword={quote_plus(keyword)}"
        ),
        (
            "https://www.work24.go.kr/wk/a/b/1200/retriveDtlEmpSrchList.do"
            f"?searchMode=Y&currentPageNo=1&pageIndex=1&sortField=DATE&sortOrderBy=DESC"
            f"&resultCnt=10&siteClcd=all&srcKeyword={quote_plus(keyword)}"
        ),
    ]

    html = ""
    used_url = ""
    for url in urls:
        try:
            html = fetch_html(url, referer="https://www.work24.go.kr/cm/main.do")
            used_url = url
            if "/wk/a/b/1500/empDetailAuthView.do" in html:
                break
        except Exception:
            continue

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if "/wk/a/b/1500/empDetailAuthView.do" not in href:
            continue
        if href.startswith("javascript:"):
            continue
        if href.startswith("#"):
            continue

        title = clean_title(a.get_text(" ", strip=True))
        if len(title) < 6:
            continue

        url_abs = urljoin(used_url, href)
        if url_abs in seen:
            continue
        seen.add(url_abs)

        block_text = ""
        node = a
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            txt = clean_text(node.get_text("\n", strip=True))
            if title in txt:
                block_text = txt
                if "등록일" in txt or "마감일" in txt:
                    break

        jobs.append(
            {
                "source": "work24",
                "keyword": keyword,
                "title": title,
                "company": "-",
                "region": "-",
                "reg_dt": None,
                "reg_date_text": "-",
                "close_date_text": "-",
                "url": url_abs,
                "search_block_text": block_text,
                "search_url": used_url,
            }
        )

    return jobs


def hydrate_work24(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url") or "https://www.work24.go.kr/cm/main.do")
    soup = BeautifulSoup(html, "html.parser")
    title = extract_meta_title(soup)
    if title:
        job["title"] = title

    lines = extract_lines_from_soup(soup)

    company = extract_labeled_company(lines)
    reg_dt, reg_text = extract_labeled_date(lines, ["등록일"])
    close_dt, close_text = extract_labeled_date(lines, ["마감일", "접수마감", "마감", "접수기간"])

    if reg_dt is None and job.get("search_block_text"):
        search_lines = unique_keep_order(
            [clean_text(x) for x in job["search_block_text"].split("\n") if clean_text(x)]
        )
        reg_dt, reg_text = extract_labeled_date(search_lines, ["등록일"])
        if close_text == "-":
            _, close_text = extract_labeled_date(search_lines, ["마감일", "접수마감", "마감", "접수기간"])

    job["company"] = company or "-"
    job["reg_dt"] = reg_dt
    job["reg_date_text"] = reg_text if reg_text != "-" else format_date(reg_dt)
    job["close_date_text"] = close_text if close_text != "-" else "-"
    return job


# -------------------------
# 사람인
# -------------------------
def search_saramin(keyword: str):
    base_url = "https://www.saramin.co.kr"
    url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={quote_plus(keyword)}&recruitPage=1"

    html = fetch_html(url, referer="https://www.saramin.co.kr/")
    soup = BeautifulSoup(html, "html.parser")
    candidates = extract_candidates_by_href(soup, "/zf_user/jobs/relay/view", base_url)

    jobs = []
    for item in candidates[:8]:
        jobs.append(
            {
                "source": "saramin",
                "keyword": keyword,
                "title": item["title"],
                "company": "-",
                "region": "-",
                "reg_dt": None,
                "reg_date_text": "-",
                "close_date_text": "-",
                "url": item["url"],
                "search_block_text": item.get("block_text", ""),
                "search_url": url,
            }
        )
    return jobs


def hydrate_saramin(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url") or "https://www.saramin.co.kr/")
    soup = BeautifulSoup(html, "html.parser")

    title = extract_meta_title(soup)
    if title:
        title = title.replace(" - 사람인", "").strip()
        job["title"] = title

    company = "-"
    for selector in [
        ".company_name",
        ".corp_name",
        ".recruit_company",
        "a[href*='/company-info/view']",
        ".company",
    ]:
        el = soup.select_one(selector)
        if el:
            company = clean_text(el.get_text(" ", strip=True))
            if company:
                break

    lines = extract_lines_from_soup(soup)
    if company == "-":
        company = extract_labeled_company(lines)

    reg_dt, reg_text = extract_labeled_date(lines, ["등록일", "수정일"])
    close_dt, close_text = extract_labeled_date(lines, ["마감일", "접수마감", "접수기간", "마감"])

    if reg_dt is None and job.get("search_block_text"):
        search_lines = unique_keep_order(
            [clean_text(x) for x in job["search_block_text"].split("\n") if clean_text(x)]
        )
        reg_dt, reg_text = extract_labeled_date(search_lines, ["등록일", "수정일"])
        if close_text == "-":
            _, close_text = extract_labeled_date(search_lines, ["마감일", "접수마감", "접수기간", "마감"])

    job["company"] = company or "-"
    job["reg_dt"] = reg_dt
    job["reg_date_text"] = reg_text if reg_text != "-" else format_date(reg_dt)
    job["close_date_text"] = close_text if close_text != "-" else "-"
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
    used_url = ""
    for url in urls:
        try:
            html = fetch_html(url, referer="https://www.jobkorea.co.kr/")
            used_url = url
            if "/Recruit/GI_Read/" in html:
                break
        except Exception:
            continue

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    base_url = "https://www.jobkorea.co.kr"
    candidates = extract_candidates_by_href(soup, "/Recruit/GI_Read/", base_url)

    jobs = []
    for item in candidates[:8]:
        jobs.append(
            {
                "source": "jobkorea",
                "keyword": keyword,
                "title": item["title"],
                "company": "-",
                "region": "-",
                "reg_dt": None,
                "reg_date_text": "-",
                "close_date_text": "-",
                "url": item["url"],
                "search_block_text": item.get("block_text", ""),
                "search_url": used_url,
            }
        )
    return jobs


def hydrate_jobkorea(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url") or "https://www.jobkorea.co.kr/")
    soup = BeautifulSoup(html, "html.parser")

    title = extract_meta_title(soup)
    if title:
        title = title.replace(" - 잡코리아", "").strip()
        job["title"] = title

    company = "-"
    for selector in [
        ".company-name",
        ".coName",
        ".tplCompany",
        ".company",
        "a[href*='/Company/']",
    ]:
        el = soup.select_one(selector)
        if el:
            company = clean_text(el.get_text(" ", strip=True))
            if company:
                break

    lines = extract_lines_from_soup(soup)
    if company == "-":
        company = extract_labeled_company(lines)

    reg_dt, reg_text = extract_labeled_date(lines, ["등록일", "수정일"])
    close_dt, close_text = extract_labeled_date(lines, ["마감일", "접수마감", "접수기간", "마감"])

    if reg_dt is None and job.get("search_block_text"):
        search_lines = unique_keep_order(
            [clean_text(x) for x in job["search_block_text"].split("\n") if clean_text(x)]
        )
        reg_dt, reg_text = extract_labeled_date(search_lines, ["등록일", "수정일"])
        if close_text == "-":
            _, close_text = extract_labeled_date(search_lines, ["마감일", "접수마감", "접수기간", "마감"])

    job["company"] = company or "-"
    job["reg_dt"] = reg_dt
    job["reg_date_text"] = reg_text if reg_text != "-" else format_date(reg_dt)
    job["close_date_text"] = close_text if close_text != "-" else "-"
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
        "work24": hydrate_work24,
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
                    job = hydrators[source](job)
                    jitter()
                except Exception as e:
                    errors.append(f"{SOURCE_LABELS[source]} | {keyword} | 상세파싱실패: {e}")
                    continue

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
