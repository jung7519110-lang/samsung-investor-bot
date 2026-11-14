"""
반도체·AI 뉴스 속보 텔레그램 알림 (15분 묶음)

- 구글 뉴스 RSS에서 최근 기사를 모은 뒤 3중 필터로 거릅니다.
  1) 회사·주제 키워드 + 중요 사건 키워드가 함께 있어야 통과
  2) 잡음 단어(칼럼, 포토, 추천주 등)가 있으면 제외
  3) 믿을 만한 언론사만 + 비슷한 제목 중복 제거
- DART_API_KEY가 있으면 관심 종목 공시도 함께 보냅니다(거르지 않음).
- 이미 보낸 기사는 seen.json에 기록해 다시 보내지 않습니다.

필요한 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수: DART_API_KEY, DRY_RUN=1
"""

import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "90"))   # 이 시간 안에 나온 기사만 대상
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "15"))          # 한 번에 보낼 최대 기사 수

# ---------- 필터 설정 (원하면 여기만 고치면 됩니다) ----------
TOPICS_KO = ["삼성전자", "SK하이닉스", "하이닉스", "엔비디아", "TSMC", "마이크론", "HBM", "반도체", "AI칩", "AI 칩", "AI반도체", "AI 반도체"]
EVENTS_KO = ["속보", "수주", "공급계약", "공급 계약", "실적", "어닝", "가이던스", "인수", "합병", "M&A", "투자", "증설",
             "감산", "수출통제", "수출 통제", "규제", "관세", "제재", "파업", "화재", "리콜", "양산", "승인", "퀄테스트", "납품"]
TOPICS_EN = ["Nvidia", "TSMC", "SK Hynix", "Hynix", "Samsung Electronics", "Micron", "HBM", "semiconductor", "chipmaker", "AI chip"]
EVENTS_EN = ["breaking", "deal", "contract", "earnings", "results", "guidance", "forecast", "acquire", "acquisition", "merger",
             "invest", "expansion", "export control", "curb", "ban", "tariff", "sanction", "strike", "fire", "recall",
             "mass production", "approval", "supply"]
NOISE = ["칼럼", "사설", "기고", "포토", "[사진]", "영상", "인터뷰", "이벤트", "추천주", "리딩방", "무료", "운세", "오피니언",
         "기자수첩", "특징주", "급등주", "테마주", "photo", "video", "opinion", "column", "podcast", "sponsored"]
SOURCES = ["연합뉴스", "연합인포맥스", "한국경제", "매일경제", "머니투데이", "이데일리", "서울경제", "조선비즈", "뉴스1", "뉴시스",
           "아시아경제", "헤럴드경제", "파이낸셜뉴스", "전자신문", "디일렉", "ZDNet Korea", "지디넷코리아", "비즈니스포스트",
           "Reuters", "Bloomberg", "CNBC", "The Wall Street Journal", "WSJ", "Financial Times", "Nikkei Asia", "Barron's"]

# 호재/악재 판단용 단어 (제목에 들어간 단어로 판단하는 간단한 방식)
GOOD_KO = ["수주", "공급계약", "공급 계약", "계약 체결", "사상 최대", "역대 최대", "최대 실적", "흑자", "호실적", "어닝 서프라이즈",
           "상회", "승인", "통과", "퀄테스트 통과", "증설", "양산", "급등", "상향", "반등", "신고가", "확대", "돌파", "수혜", "완화", "해제"]
BAD_KO = ["감산", "적자", "하회", "쇼크", "부진", "규제", "제재", "수출통제", "수출 통제", "관세", "파업", "화재", "리콜", "급락",
          "하향", "소송", "지연", "실패", "차질", "중단", "탈락", "우려", "경고", "축소", "철회", "신저가", "손실"]
GOOD_EN = ["beat", "beats", "record", "surge", "soar", "jump", "wins", "win ", "approval", "approved", "raises", "upgrade",
           "rally", "expands", "boost", "strong demand", "eases", "lifts"]
BAD_EN = ["export control", "miss", "misses", "plunge", "slump", "tumble", "cut", "ban", "curb", "sanction", "tariff", "strike", "fire", "recall",
          "lawsuit", "delay", "downgrade", "warns", "weak", "loss", "probe", "halt", "falls", "drops"]
GOOD_DART = ["공급계약", "수주", "자기주식취득", "현금ㆍ현물배당", "현금배당", "무상증자"]
BAD_DART = ["유상증자", "소송", "횡령", "배임", "영업정지", "자기주식처분", "감자"]


def judge(title: str, good: list[str], bad: list[str]) -> str:
    """제목 단어로 호재/악재/중립을 판단 (참고용)"""
    t = title.lower()
    g = sum(1 for w in good if w.lower() in t)
    b = sum(1 for w in bad if w.lower() in t)
    if g > b:
        return "🔴 호재"
    if b > g:
        return "🔵 악재"
    return "⚪ 중립"


# 관심 종목 공시 (DART 고유번호)
DART_CORPS = {"00126380": "삼성전자", "00164779": "SK하이닉스"}

FEEDS = [
    # (검색어, 언어설정)
    ("삼성전자 OR SK하이닉스 OR 하이닉스", "ko"),
    ("엔비디아 OR TSMC OR 마이크론", "ko"),
    ("반도체 OR HBM OR AI반도체 OR \"AI 칩\"", "ko"),
    ("Nvidia OR TSMC OR \"SK Hynix\" OR Micron", "en"),
    ("semiconductor OR chipmaker OR HBM OR \"AI chip\"", "en"),
]


# ---------- 수집 ----------
def feed_url(query: str, lang: str) -> str:
    q = urllib.parse.quote(f"{query} when:1h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def parse_feed(xml_text: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # 구글 뉴스 제목 끝의 " - 언론사" 제거
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        pub = it.findtext("pubDate") or ""
        try:
            dt = email.utils.parsedate_to_datetime(pub).astimezone(KST)
        except Exception:
            dt = None
        items.append({
            "id": (it.findtext("guid") or it.findtext("link") or title).strip(),
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "source": source,
            "time": dt,
        })
    return items


def fetch_news() -> list[dict]:
    out = []
    for query, lang in FEEDS:
        try:
            r = requests.get(feed_url(query, lang), timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            for item in parse_feed(r.text):
                item["lang"] = lang
                out.append(item)
        except Exception as e:
            print(f"피드 오류({query}): {e}", file=sys.stderr)
        time.sleep(1)
    return out


# ---------- 필터 ----------
def has_any(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w.lower() in t for w in words)


def source_ok(source: str) -> bool:
    return any(s.lower() in source.lower() for s in SOURCES)


def is_important(item: dict) -> bool:
    title = item["title"]
    if not title or has_any(title, NOISE):
        return False
    if not source_ok(item["source"]):
        return False
    if item["lang"] == "ko":
        return has_any(title, TOPICS_KO) and has_any(title, EVENTS_KO)
    return has_any(title, TOPICS_EN) and has_any(title, EVENTS_EN)


def tokens(title: str) -> set[str]:
    t = re.sub(r"[\[\]\(\)\"'“”‘’…·,.!?:;|/\-]", " ", title.lower())
    return {w for w in t.split() if len(w) >= 2}


def similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.6


# ---------- 기록 ----------
def load_seen() -> dict:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("ids", {})
    data.setdefault("titles", [])
    # 2일 지난 기록은 정리
    cutoff = time.time() - 2 * 86400
    data["ids"] = {k: v for k, v in data["ids"].items() if v > cutoff}
    data["titles"] = [t for t in data["titles"] if t[1] > cutoff]
    return data


def save_seen(data: dict) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def select_news(items: list[dict], seen: dict, now: datetime) -> list[dict]:
    cutoff = now - timedelta(minutes=LOOKBACK_MIN)
    seen_titles = [set(t[0].split("\u0001")) for t in seen["titles"]]
    picked, picked_tokens = [], []
    for it in sorted(items, key=lambda x: x["time"] or now, reverse=True):
        if it["id"] in seen["ids"]:
            continue
        if it["time"] and it["time"] < cutoff:
            continue
        if not is_important(it):
            continue
        tk = tokens(it["title"])
        if any(similar(tk, s) for s in seen_titles + picked_tokens):
            seen["ids"][it["id"]] = time.time()   # 비슷한 기사는 보낸 것으로 처리
            continue
        picked.append(it)
        picked_tokens.append(tk)
    return picked[:MAX_ITEMS]


# ---------- DART 공시 ----------
def fetch_dart(api_key: str, seen: dict, now: datetime) -> list[dict]:
    out = []
    today = now.strftime("%Y%m%d")
    for corp, name in DART_CORPS.items():
        try:
            r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20, params={
                "crtfc_key": api_key, "corp_code": corp, "bgn_de": today, "end_de": today, "page_count": 20})
            data = r.json()
            for d in data.get("list", []):
                rid = "dart:" + d["rcept_no"]
                if rid in seen["ids"]:
                    continue
                out.append({"id": rid, "name": name, "title": d.get("report_nm", "").strip(),
                            "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['rcept_no']}"})
        except Exception as e:
            print(f"DART 오류({name}): {e}", file=sys.stderr)
    return out


# ---------- 전송 ----------
QUIET_START = int(os.getenv("QUIET_START", "22"))   # 이 시각부터
QUIET_END = int(os.getenv("QUIET_END", "6"))        # 이 시각 전까지는 보내지 않고 모아둠
MORNING_MAX = int(os.getenv("MORNING_MAX", "40"))   # 아침 몰아보기 최대 기사 수


def is_quiet(now: datetime) -> bool:
    return now.hour >= QUIET_START or now.hour < QUIET_END


def build_message(news: list[dict], disclosures: list[dict], now: datetime, title: str | None = None) -> list[str]:
    """텔레그램 글자 수 제한(4096자)을 넘으면 여러 개로 나눠서 돌려줌"""
    head = title or f"📰 <b>반도체·AI 속보</b> ({now:%m/%d %H:%M})"
    body, tags = [], []
    if disclosures:
        body.append("\n<b>📢 공시</b>")
        for d in disclosures:
            tag = judge(d["title"], GOOD_DART, BAD_DART)
            tags.append(tag)
            body.append(f"{tag} <b>{html.escape(d['name'])}</b> "
                        f"<a href=\"{html.escape(d['link'])}\">{html.escape(d['title'])}</a>")
    if news:
        body.append("\n<b>🗞 뉴스</b>")
        for n in news:
            if n["lang"] == "en":
                tag = judge(n["title"], GOOD_EN, BAD_EN)
            else:
                tag = judge(n["title"], GOOD_KO, BAD_KO)
            tags.append(tag)
            t = f"{n['time']:%m/%d %H:%M} " if (n["time"] and title) else (f"{n['time']:%H:%M} " if n["time"] else "")
            flag = "🌐 " if n["lang"] == "en" else ""
            body.append(f"{tag} {t}{flag}<a href=\"{html.escape(n['link'])}\">{html.escape(n['title'])}</a> "
                        f"<i>{html.escape(n['source'])}</i>")
    good = sum(1 for x in tags if "호재" in x)
    bad = sum(1 for x in tags if "악재" in x)
    neutral = len(tags) - good - bad
    summary = f"요약: 🔴 호재 {good} · 🔵 악재 {bad} · ⚪ 중립 {neutral}"
    foot = "\n<i>※ 호재/악재는 제목 단어로 자동 판단한 참고용입니다.</i>"

    chunks, cur = [], [head, summary]
    for line in body:
        if len("\n".join(cur + [line, foot])) > 3900:
            chunks.append("\n".join(cur))
            cur = [head + " (이어서)"]
        cur.append(line)
    cur.append(foot)
    chunks.append("\n".join(cur))
    return chunks


def send_telegram(text: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        print(text)
        print("-" * 40)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")


# 밤사이 모아둔 항목 저장/복원
def to_store(n: dict) -> dict:
    d = dict(n)
    if hasattr(d.get("time"), "isoformat"):
        d["time"] = d["time"].isoformat()
    return d


def from_store(d: dict) -> dict:
    n = dict(d)
    if n.get("time"):
        try:
            n["time"] = datetime.fromisoformat(n["time"])
        except Exception:
            n["time"] = None
    return n


def main() -> int:
    now = datetime.now(KST)
    seen = load_seen()
    seen.setdefault("pending_news", [])
    seen.setdefault("pending_dart", [])
    first_run = not seen["ids"]

    news = select_news(fetch_news(), seen, now)
    dart_key = os.getenv("DART_API_KEY", "").strip()
    disclosures = fetch_dart(dart_key, seen, now) if dart_key else []

    # 새로 찾은 항목은 바로 '본 것'으로 기록 (다시 찾지 않도록)
    for n in news:
        seen["ids"][n["id"]] = time.time()
        seen["titles"].append(["\u0001".join(sorted(tokens(n["title"]))), time.time()])
    for d in disclosures:
        seen["ids"][d["id"]] = time.time()

    if is_quiet(now):
        # 밤에는 보내지 않고 모아두기
        seen["pending_news"] += [to_store(n) for n in news]
        seen["pending_dart"] += disclosures
        print(f"조용한 시간: 뉴스 {len(news)}건, 공시 {len(disclosures)}건 보관 "
              f"(누적 뉴스 {len(seen['pending_news'])}, 공시 {len(seen['pending_dart'])})")
    else:
        pend_news = [from_store(x) for x in seen["pending_news"]]
        pend_dart = seen["pending_dart"]
        if pend_news or pend_dart:
            # 아침 몰아보기: 밤사이 모인 것 + 지금 새로 찾은 것
            all_news = sorted(pend_news + news, key=lambda x: x["time"] or now)[-MORNING_MAX:]
            title = f"🌙 <b>밤사이 반도체·AI 소식</b> ({QUIET_START}:00~{now:%H:%M})"
            for msg in build_message(all_news, pend_dart + disclosures, now, title):
                send_telegram(msg)
            seen["pending_news"], seen["pending_dart"] = [], []
        elif news or disclosures:
            for msg in build_message(news, disclosures, now):
                send_telegram(msg)
        else:
            print("새로 보낼 기사 없음")

    save_seen(seen)
    if first_run:
        print("첫 실행: 기록 파일을 새로 만들었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
