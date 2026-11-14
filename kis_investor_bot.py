"""
삼성전자 투자자별 매매동향 텔레그램 알림
- 데이터: 한국투자증권 Open API  /quotations/inquire-investor (tr_id FHKST01010900)
- 전송: 텔레그램 봇

필요한 환경변수 (GitHub Secrets로 등록)
  KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수
  STOCKS (기본 "005930:삼성전자,000660:SK하이닉스"), DAYS (기본 7)
  DRY_RUN=1  → 텔레그램으로 보내지 않고 화면에만 출력
"""

import html
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

KIS_BASE = "https://openapi.koreainvestment.com:9443"
KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"

# "종목코드:이름" 을 쉼표로 구분
STOCKS = [tuple(x.split(":", 1)) for x in os.getenv("STOCKS", "005930:삼성전자,000660:SK하이닉스").split(",") if x.strip()]
DAYS = int(os.getenv("DAYS", "7"))

INVESTORS = [("개인", "prsn"), ("외국인", "frgn"), ("기관", "orgn")]


# ---------- KIS API ----------
def get_token(app_key: str, app_secret: str) -> str:
    r = requests.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"토큰 발급 실패: {data}")
    return data["access_token"]


def fetch_investor(token: str, app_key: str, app_secret: str, code: str) -> list[dict]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    r = requests.get(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers=headers, params=params, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"조회 실패: {data.get('msg_cd')} {data.get('msg1')}")
    return data.get("output") or []


# ---------- 가공 ----------
def to_int(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def man(q: int, signed: bool = False) -> str:
    """주 → 만주 (소수 1자리)"""
    v = round(q / 10000, 1) + 0.0  # -0.0 방지
    if v == 0:
        v = 0.0
    return f"{v:+,.1f}" if signed and v != 0 else f"{v:,.1f}"


def parse_rows(output: list[dict]) -> list[dict]:
    rows = []
    for o in output:
        date = (o.get("stck_bsop_date") or "").strip()
        if not date:
            continue
        row = {
            "date": date,
            "close": to_int(o.get("stck_clpr")),
            "change": to_int(o.get("prdy_vrss")),
            "sign": str(o.get("prdy_vrss_sign", "3")),
        }
        for _, p in INVESTORS:
            row[f"{p}_buy"] = to_int(o.get(f"{p}_shnu_vol"))
            row[f"{p}_sell"] = to_int(o.get(f"{p}_seln_vol"))
            row[f"{p}_net"] = to_int(o.get(f"{p}_ntby_qty"))
        rows.append(row)
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:DAYS]


def pct_change(r: dict) -> float:
    if r["sign"] in ("1", "2"):
        prev = r["close"] - r["change"]
    elif r["sign"] in ("4", "5"):
        prev = r["close"] + r["change"]
    else:
        prev = r["close"]
    return (r["close"] / prev - 1) * 100 if prev else 0.0


def build_caption(rows: list[dict], now: datetime, code: str, name_: str) -> str:
    lines = [
        f"📊 <b>{html.escape(name_)}({code}) 투자자별 매매동향</b>",
        f"{now:%Y-%m-%d %H:%M} 기준 · 최근 {len(rows)}거래일",
        "",
        "<b>기간 순매수 합계 (만주)</b>",
    ]
    for name, p in INVESTORS:
        total = sum(r[f"{p}_net"] for r in rows)
        icon = "🔴" if total > 0 else "🔵" if total < 0 else "⚪"
        lines.append(f"{icon} {name} {man(total, True)}")
    today = now.strftime("%Y%m%d")
    if now.weekday() < 5 and rows and rows[0]["date"] != today:
        lines.append("\n※ 당일 수치는 장 종료 후 제공돼 아직 포함되지 않았습니다(휴장일이면 무시).")
    return "\n".join(lines)


def _korean_font():
    from matplotlib import font_manager
    candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
            return font_manager.FontProperties(fname=path).get_name()
    for f in font_manager.fontManager.ttflist:
        if any(k in f.name for k in ("Nanum", "Noto Sans CJK KR", "Noto Sans KR", "Malgun", "AppleGothic")):
            return f.name
    return None


def build_table_image(rows: list[dict], now: datetime, code: str, name_: str, path: str = "table.png") -> str:
    """일자별 개인·외국인·기관 매수/매도/순매수 표를 PNG로 저장"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    font = _korean_font()
    if font:
        plt.rcParams["font.family"] = font
    plt.rcParams["axes.unicode_minus"] = False

    RED, BLUE, GRAY = "#d62728", "#1f5fbf", "#333333"
    HEAD_BG, SUB_BG, TOTAL_BG, LINE = "#2f3e55", "#e8ecf2", "#fff6d6", "#c9d1dc"
    GROUP_BG = {"개인": "#eef6ee", "외국인": "#eef2fb", "기관": "#fbf0ee"}

    # 열 정의: (제목, 너비)
    cols = [("날짜", 1.25), ("종가", 1.15), ("등락률", 1.0)]
    for name, _ in INVESTORS:
        cols += [(f"{name}|매수", 1.0), (f"{name}|매도", 1.0), (f"{name}|순매수", 1.1)]
    widths = [w for _, w in cols]
    xs = [sum(widths[:i]) for i in range(len(widths) + 1)]
    W = xs[-1]

    n = len(rows)
    row_h, head_h = 1.0, 0.9
    title_h = 1.5
    H = title_h + head_h * 2 + row_h * (n + 1) + 0.6

    fig = plt.figure(figsize=(W * 0.95, H * 0.55), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")

    def cell(ci, y, h, text, bg=None, color=GRAY, weight="normal", size=10.5, span=1):
        x0, x1 = xs[ci], xs[ci + span]
        if bg:
            ax.add_patch(Rectangle((x0, y), x1 - x0, h, facecolor=bg, edgecolor=LINE, linewidth=0.6))
        else:
            ax.add_patch(Rectangle((x0, y), x1 - x0, h, facecolor="white", edgecolor=LINE, linewidth=0.6))
        ax.text((x0 + x1) / 2, y + h / 2, text, ha="center", va="center",
                color=color, fontsize=size, fontweight=weight)

    # 제목
    ax.text(0.15, 0.55, f"{name_}({code}) 투자자별 매매동향",
            fontsize=16, fontweight="bold", color="#111111", va="center")
    ax.text(0.15, 1.15, f"{now:%Y-%m-%d %H:%M} 기준 · 최근 {n}거래일 · 단위: 만주 (빨강=순매수, 파랑=순매도)",
            fontsize=10, color="#666666", va="center")

    # 헤더 1행 (그룹)
    y = title_h
    for ci, title in enumerate(["날짜", "종가", "등락률"]):
        cell(ci, y, head_h * 2, title, bg=HEAD_BG, color="white", weight="bold", size=11)
    for gi, (name, _) in enumerate(INVESTORS):
        cell(3 + gi * 3, y, head_h, name, bg=HEAD_BG, color="white", weight="bold", size=11.5, span=3)
    # 헤더 2행 (매수/매도/순매수)
    y += head_h
    for gi in range(len(INVESTORS)):
        for k, sub in enumerate(["매수", "매도", "순매수"]):
            cell(3 + gi * 3 + k, y, head_h, sub, bg=SUB_BG, weight="bold", size=10.5)
    y += head_h

    def signed_color(v):
        return RED if v > 0 else BLUE if v < 0 else GRAY

    # 데이터 행
    for r in rows:
        d = datetime.strptime(r["date"], "%Y%m%d")
        pct = pct_change(r)
        cell(0, y, row_h, f"{d:%m/%d}({WEEKDAYS[d.weekday()]})", weight="bold")
        cell(1, y, row_h, f"{r['close']:,}")
        cell(2, y, row_h, f"{pct:+.2f}%", color=signed_color(pct))
        for gi, (name, p) in enumerate(INVESTORS):
            bg = GROUP_BG[name]
            cell(3 + gi * 3, y, row_h, man(r[p + "_buy"]), bg=bg)
            cell(4 + gi * 3, y, row_h, man(r[p + "_sell"]), bg=bg)
            net = r[p + "_net"]
            cell(5 + gi * 3, y, row_h, man(net, True), bg=bg, color=signed_color(net), weight="bold")
        y += row_h

    # 합계 행
    cell(0, y, row_h, "합계", bg=TOTAL_BG, weight="bold", span=3)
    for gi, (name, p) in enumerate(INVESTORS):
        tb = sum(r[p + "_buy"] for r in rows)
        ts = sum(r[p + "_sell"] for r in rows)
        tn = sum(r[p + "_net"] for r in rows)
        cell(3 + gi * 3, y, row_h, man(tb), bg=TOTAL_BG, weight="bold")
        cell(4 + gi * 3, y, row_h, man(ts), bg=TOTAL_BG, weight="bold")
        cell(5 + gi * 3, y, row_h, man(tn, True), bg=TOTAL_BG, color=signed_color(tn), weight="bold")

    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)
    return path


# ---------- 텔레그램 ----------
def send_telegram(text: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")


def send_telegram_photo(path: str, caption: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        print(f"[이미지] {path}\n{caption}")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f},
            timeout=30,
        )
    if not r.ok:
        raise RuntimeError(f"텔레그램 사진 전송 실패: {r.status_code} {r.text}")


def main() -> int:
    now = datetime.now(KST)
    try:
        key, secret = os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]
        token = get_token(key, secret)
    except Exception as e:
        msg = f"⚠️ 매매동향 알림 실패 ({now:%m/%d %H:%M})\n토큰 발급 오류: {e}"
        print(msg, file=sys.stderr)
        send_telegram(html.escape(msg))
        return 1

    failed = 0
    for code, name_ in STOCKS:
        try:
            rows = parse_rows(fetch_investor(token, key, secret, code))
            if not rows:
                raise RuntimeError("응답에 데이터가 없습니다.")
            img = build_table_image(rows, now, code, name_, path=f"table_{code}.png")
            send_telegram_photo(img, build_caption(rows, now, code, name_))
        except Exception as e:  # 한 종목이 실패해도 나머지는 계속
            failed += 1
            msg = f"⚠️ {name_}({code}) 매매동향 알림 실패 ({now:%m/%d %H:%M})\n{e}"
            print(msg, file=sys.stderr)
            try:
                send_telegram(html.escape(msg))
            except Exception as e2:
                print(f"오류 알림도 실패: {e2}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
