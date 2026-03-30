import os
import re
import json
import time
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

BAD_TITLES = {
    "본문 바로가기",
    "주메뉴 바로가기",
    "푸터 바로가기",
    "메뉴 바로가기",
}

BAD_COMPANY_WORDS = [
    "등록일", "수정일", "마감일", "근무지", "경력", "학력", "급여", "지원",
    "스크랩", "관심기업", "기업정보", "상세요강", "채용공고", "모집요강",
    "기간", "방법", "즉시지원", "파견", "정규직", "계약직", "아르바이트",
    "공고", "채용", "상세", "모집", "본문", "주메뉴", "바로가기",
]

SESSION = requests.Session()
retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "HEAD"]),
)
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "close",
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


def split_lines(text: str):
    return [clean_text(x) for x in re.split(r"[\n\r]+", text) if clean_text(x)]


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


def fetch_html(url: str, referer: str | None = None, allow_insecure_retry: bool = False) -> str:
    headers = {}
    if referer:
        headers["Referer"] = referer

    try:
        response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text
    except requests.exceptions.SSLError:
        if not allow_insecure_retry:
            raise
        response = SESSION.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        response.raise_for_status()
        return response.text


def parse_date_from_text(text: str):
    if not text:
        return None

    text = clean_text(text)

    if "오늘" in text or "금일" in text or "방금" in text:
        return now_kst()

    if "어제" in text:
        return now_kst() - timedelta(days=1)

    m = re.search(r"(\d+)\s*일\s*전", text)
    if m:
        return now_kst() - timedelta(days=int(m.group(1)))

    m = re.search(r"(\d+)\s*시간\s*전", text)
    if m:
        return now_kst()

    patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일",
        r"~\s*(\d{2})\.(\d{2})",
        r"(\d{2})/(\d{2})/(\d{2})",
    ]

    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue

        parts = match.groups()

        try:
            if idx in (0, 1):
                year, month, day = map(int, parts)
            elif idx == 2:
                year = now_kst().year
                month = int(parts[0])
                day = int(parts[1])
            else:
                year = 2000 + int(parts[0])
                month = int(parts[1])
                day = int(parts[2])

            dt = datetime(year, month, day, tzinfo=KST)

            if idx == 2 and dt < now_kst() - timedelta(days=330):
                dt = datetime(year + 1, month, day, tzinfo=KST)

            return dt
        except ValueError:
            return None

    return None


def extract_labeled_field(text: str, label: str) -> str:
    if not text:
        return "-"

    patterns = [
        rf"{label}\s*[:：]?\s*(20\d{{2}}[./-]\d{{1,2}}[./-]\d{{1,2}})",
        rf"{label}\s*[:：]?\s*(20\d{{2}}년\s*\d{{1,2}}월\s*\d{{1,2}}일)",
        rf"{label}\s*[:：]?\s*(오늘|어제|금일|방금|\d+\s*일\s*전|\d+\s*시간\s*전)",
        rf"{label}\s*[:：]?\s*(~\s*\d{{2}}\.\d{{2}}(?:\([^)]+\))?)",
        rf"{label}\s*[:：]?\s*(D-\d+|오늘마감|내일마감|상시채용|채용시|접수마감)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return clean_text(m.group(1))

    return "-"


def extract_deadline_from_text(text: str) -> str:
    if not text:
        return "-"

    labeled = extract_labeled_field(text, "마감일")
    if labeled != "-":
        return labeled

    patterns = [
        r"(~\s*\d{2}\.\d{2}(?:\([^)]+\))?)",
        r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
        r"(20\d{2}년\s*\d{1,2}월\s*\d{1,2}일)",
        r"(D-\d+|오늘마감|내일마감|상시채용|채용시|접수마감)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return clean_text(m.group(1))

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


def format_date(dt) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d")


def is_bad_title(title: str) -> bool:
    title = clean_text(title)
    if not title or len(title) < 4:
        return True
    if title in BAD_TITLES:
        return True
    if "바로가기" in title:
        return True
    return False


def is_valid_href(href: str) -> bool:
    href = (href or "").strip()
    if not href:
        return False
    if href.startswith("javascript:"):
        return False
    if href.startswith("#"):
        return False
    return True


def value_after_label(lines, labels):
    for i, line in enumerate(lines):
        for label in labels:
            if line.startswith(label):
                value = clean_text(re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", line))
                if value and value != line:
                    return value
                if i + 1 < len(lines):
                    nxt = clean_text(lines[i + 1])
                    if nxt and all(word not in nxt for word in BAD_COMPANY_WORDS) and len(nxt) <= 50:
                        return nxt
    return "-"


def company_from_title(title: str) -> str:
    m = re.match(r"^\[([^\]]+)\]", clean_text(title))
    if m:
        return clean_text(m.group(1))
    return "-"


def pick_company_from_lines(lines, title):
    by_label = value_after_label(lines, ["회사명", "기업명", "사업장명", "업체명", "기관명", "상호"])
    if by_label != "-":
        return by_label

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
        if any(word in line for word in BAD_COMPANY_WORDS):
            continue
        if len(line) > 50:
            continue
        return line

    return company_from_title(title)


def nearest_block_text(anchor, max_hops=6):
    node = anchor
    best_text = ""
    for _ in range(max_hops):
        node = node.parent
        if node is None:
            break
        txt = node.get_text("\n", strip=True)
        lines = split_lines(txt)
        if 2 <= len(lines) <= 30:
            best_text = "\n".join(lines)
            break
    return best_text


# -------------------------
# 고용24
# -------------------------
def search_work24(keyword: str):
    base_url = "https://m.work24.go.kr"
    search_url = (
        "https://m.work24.go.kr/wk/a/b/1200/retriveDtlEmpSrchList.do"
        f"?searchMode=Y&currentPageNo=1&pageIndex=1&sortField=DATE&sortOrderBy=DESC"
        f"&resultCnt=10&siteClcd=all&srcKeyword={quote_plus(keyword)}"
    )

    html = fetch_html(search_url, allow_insecure_retry=True)
    soup = BeautifulSoup(html, "html.parser")

    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not is_valid_href(href):
            continue
        if "/wk/a/b/1500/empDetailAuthView.do" not in href:
            continue
        if "wantedAuthNo=" not in href:
            continue

        url_abs = urljoin(base_url, href)
        if url_abs in seen:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if is_bad_title(title):
            continue

        block_text = nearest_block_text(a)
        lines = split_lines(block_text)

        jobs.append(
            {
                "source": "work24",
                "keyword": keyword,
                "title": title,
                "company": pick_company_from_lines(lines, title),
                "region": "-",
                "reg_dt": None,
                "reg_date_text": "-",
                "close_date_text": "-",
                "url": url_abs,
                "search_url": search_url,
                "needs_detail": True,
            }
        )
        seen.add(url_abs)

    return jobs[:8]


def hydrate_work24(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url"), allow_insecure_retry=True)
    soup = BeautifulSoup(html, "html.parser")

    raw_text = soup.get_text("\n", strip=True)
    lines = split_lines(raw_text)

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        job["title"] = clean_text(og_title["content"])

    if is_bad_title(job.get("title", "")):
        for h in soup.find_all(["h1", "h2", "strong", "title"]):
            candidate = clean_text(h.get_text(" ", strip=True))
            if not is_bad_title(candidate):
                job["title"] = candidate
                break

    company = pick_company_from_lines(lines, job["title"])
    if company == "-":
        company = company_from_title(job["title"])

    reg_text = extract_labeled_field(raw_text, "등록일")
    close_text = extract_labeled_field(raw_text, "마감일")
    reg_dt = parse_date_from_text(reg_text)

    job["company"] = company or "-"
    job["reg_dt"] = reg_dt
    job["reg_date_text"] = format_date(reg_dt)
    job["close_date_text"] = close_text if close_text != "-" else extract_deadline_from_text(raw_text)
    job["needs_detail"] = False
    return job


# -------------------------
# 사람인
# -------------------------
def search_saramin(keyword: str):
    base_url = "https://www.saramin.co.kr"
    search_url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={quote_plus(keyword)}&recruitPage=1"

    html = fetch_html(search_url)
    soup = BeautifulSoup(html, "html.parser")

    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not is_valid_href(href):
            continue
        if "/zf_user/jobs/relay/view" not in href:
            continue
        if "rec_idx=" not in href:
            continue

        url_abs = urljoin(base_url, href)
        if url_abs in seen:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if is_bad_title(title):
            continue

        block_text = nearest_block_text(a)
        lines = split_lines(block_text)
        reg_dt = parse_date_from_text(block_text)

        jobs.append(
            {
                "source": "saramin",
                "keyword": keyword,
                "title": title,
                "company": pick_company_from_lines(lines, title),
                "region": "-",
                "reg_dt": reg_dt,
                "reg_date_text": format_date(reg_dt),
                "close_date_text": extract_deadline_from_text(block_text),
                "url": url_abs,
                "search_url": search_url,
                "needs_detail": reg_dt is None,
            }
        )
        seen.add(url_abs)

    return jobs[:8]


def hydrate_saramin(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url"))
    soup = BeautifulSoup(html, "html.parser")

    raw_text = soup.get_text("\n", strip=True)
    lines = split_lines(raw_text)

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        job["title"] = clean_text(og_title["content"])

    for selector in [
        ".company_name",
        ".corp_name",
        ".recruit_company",
        "a[href*='/company-info/view']",
    ]:
        el = soup.select_one(selector)
        if el:
            candidate = clean_text(el.get_text(" ", strip=True))
            if candidate:
                job["company"] = candidate
                break

    if job.get("company", "-") == "-":
        job["company"] = pick_company_from_lines(lines, job["title"])

    if job.get("reg_dt") is None:
        job["reg_dt"] = parse_date_from_text(raw_text)
        job["reg_date_text"] = format_date(job["reg_dt"])

    if job.get("close_date_text", "-") == "-":
        job["close_date_text"] = extract_deadline_from_text(raw_text)

    job["needs_detail"] = False
    return job


# -------------------------
# 잡코리아
# -------------------------
def search_jobkorea(keyword: str):
    urls = [
        f"https://m.jobkorea.co.kr/Search/?stext={quote_plus(keyword)}",
        f"https://www.jobkorea.co.kr/Search/?stext={quote_plus(keyword)}",
    ]

    html = ""
    search_url = ""
    for url in urls:
        try:
            html = fetch_html(url)
            if "/Recruit/GI_Read/" in html:
                search_url = url
                break
        except Exception:
            continue

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not is_valid_href(href):
            continue
        if "/Recruit/GI_Read/" not in href:
            continue

        url_abs = urljoin("https://m.jobkorea.co.kr", href)
        url_abs = url_abs.replace("https://www.jobkorea.co.kr", "https://m.jobkorea.co.kr")

        if url_abs in seen:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if is_bad_title(title):
            continue

        block_text = nearest_block_text(a)
        lines = split_lines(block_text)
        reg_dt = parse_date_from_text(block_text)

        jobs.append(
            {
                "source": "jobkorea",
                "keyword": keyword,
                "title": title,
                "company": pick_company_from_lines(lines, title),
                "region": "-",
                "reg_dt": reg_dt,
                "reg_date_text": format_date(reg_dt),
                "close_date_text": extract_deadline_from_text(block_text),
                "url": url_abs,
                "search_url": search_url,
                "needs_detail": reg_dt is None,
            }
        )
        seen.add(url_abs)

    return jobs[:8]


def hydrate_jobkorea(job: dict) -> dict:
    html = fetch_html(job["url"], referer=job.get("search_url"))
    soup = BeautifulSoup(html, "html.parser")

    raw_text = soup.get_text("\n", strip=True)
    lines = split_lines(raw_text)

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        job["title"] = clean_text(og_title["content"])

    if "채용공고" in lines:
        try:
            idx = lines.index("채용공고")
            if idx + 1 < len(lines):
                candidate_company = clean_text(lines[idx + 1])
                if candidate_company and all(word not in candidate_company for word in BAD_COMPANY_WORDS):
                    job["company"] = candidate_company
            if idx + 2 < len(lines):
                candidate_title = clean_text(lines[idx + 2])
                if candidate_title and not is_bad_title(candidate_title):
                    job["title"] = candidate_title
        except Exception:
            pass

    if job.get("company", "-") == "-":
        job["company"] = pick_company_from_lines(lines, job["title"])

    if job.get("reg_dt") is None:
        job["reg_dt"] = parse_date_from_text(raw_text)
        job["reg_date_text"] = format_date(job["reg_dt"])

    if job.get("close_date_text", "-") == "-":
        job["close_date_text"] = extract_deadline_from_text(raw_text)

    job["needs_detail"] = False
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
                    if job.get("needs_detail", False):
                        job = hydrators[source](job)
                        jitter()
                except Exception as e:
                    errors.append(f"{SOURCE_LABELS[source]} | {keyword} | 상세파싱실패: {e}")
                    continue

                if is_bad_title(job.get("title", "")):
                    continue
                if not is_valid_href(job.get("url", "")):
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
