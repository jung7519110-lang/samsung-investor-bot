"""
삼성전자 투자자별 매매동향 텔레그램 알림
- 데이터: 한국투자증권 Open API  /quotations/inquire-investor (tr_id FHKST01010900)
- 전송: 텔레그램 봇

필요한 환경변수 (GitHub Secrets로 등록)
  KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수
  STOCK_CODE (기본 005930), STOCK_NAME (기본 삼성전자), DAYS (기본 7)
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

STOCK_CODE = os.getenv("STOCK_CODE", "005930")
STOCK_NAME = os.getenv("STOCK_NAME", "삼성전자")
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


def fetch_investor(token: str, app_key: str, app_secret: str) -> list[dict]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": STOCK_CODE}
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
    return f"{q / 10000:+,.1f}" if signed else f"{q / 10000:,.1f}"


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


def build_message(rows: list[dict], now: datetime) -> str:
    title = f"📊 {STOCK_NAME}({STOCK_CODE}) 투자자별 매매동향"
    sub = f"{now:%Y-%m-%d %H:%M} 기준 · 최근 {len(rows)}거래일 · 단위: 만주"

    # 기간 합계
    lines = ["[기간 순매수 합계]"]
    for name, p in INVESTORS:
        total = sum(r[f"{p}_net"] for r in rows)
        lines.append(f"{name:<3}\t{man(total, True):>10}")
    lines.append("")

    for r in rows:
        d = datetime.strptime(r["date"], "%Y%m%d")
        if r["sign"] in ("1", "2"):
            arrow = "▲"
        elif r["sign"] in ("4", "5"):
            arrow = "▼"
        else:
            arrow = "-"
        prev = r["close"] - (r["change"] if arrow == "▲" else -r["change"] if arrow == "▼" else 0)
        pct = (r["close"] / prev - 1) * 100 if prev else 0
        lines.append(f"■ {d:%m/%d}({WEEKDAYS[d.weekday()]}) 종가 {r['close']:,} {arrow}{abs(r['change']):,} ({pct:+.2f}%)")
        for name, p in INVESTORS:
            lines.append(
                f" {name:<3} 매수 {man(r[p + '_buy']):>7} 매도 {man(r[p + '_sell']):>7} 순 {man(r[p + '_net'], True):>7}"
            )
        lines.append("")

    body = "\n".join(lines).replace("\t", " ").rstrip()

    note = ""
    today = now.strftime("%Y%m%d")
    if now.weekday() < 5 and rows and rows[0]["date"] != today:
        note = "\n※ 당일 수치는 장 종료 후 제공돼 아직 포함되지 않았습니다(휴장일이면 무시)."

    return f"<b>{html.escape(title)}</b>\n{html.escape(sub)}\n<pre>{html.escape(body)}</pre>{html.escape(note)}"


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


def main() -> int:
    now = datetime.now(KST)
    try:
        key, secret = os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]
        token = get_token(key, secret)
        rows = parse_rows(fetch_investor(token, key, secret))
        if not rows:
            raise RuntimeError("응답에 데이터가 없습니다.")
        send_telegram(build_message(rows, now))
        return 0
    except Exception as e:  # 실패도 텔레그램으로 알림
        msg = f"⚠️ {STOCK_NAME} 매매동향 알림 실패 ({now:%m/%d %H:%M})\n{e}"
        print(msg, file=sys.stderr)
        try:
            send_telegram(html.escape(msg))
        except Exception as e2:
            print(f"오류 알림도 실패: {e2}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
