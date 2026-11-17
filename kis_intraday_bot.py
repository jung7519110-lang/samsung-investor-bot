"""
장중 외국인·기관 수급(추정치) 텔레그램 알림
- 데이터: 한국투자증권 Open API
    /quotations/investor-trend-estimate (tr_id HHPTJ04160200) 종목별 외인기관 추정가집계
    /quotations/inquire-price           (tr_id FHKST01010100) 현재가
- 갱신 시각(공식 안내, 10분 정도 차이 가능):
    외국인 09:30, 11:20, 13:20, 14:30 / 기관 10:00, 11:20, 13:20, 14:30
- 증권사 직원이 장중에 입력한 수치를 단순 누계한 '추정치'이며, 개인은 제공되지 않음
  (확정치는 장 마감 후 kis_investor_bot.py 가 보냄)

필요한 환경변수: KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수: STOCKS (기본 "005930:삼성전자,000660:SK하이닉스"), DRY_RUN=1
"""

import html
import os
import sys
from datetime import datetime

import requests

from kis_investor_bot import KIS_BASE, KST, STOCKS, drop_token, get_token, man, send_telegram, to_int


def _get(token: str, key: str, secret: str, path: str, tr_id: str, params: dict) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": key,
        "appsecret": secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/{path}",
                     headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"조회 실패({tr_id}): {data.get('msg_cd')} {data.get('msg1')}")
    return data


def fetch_estimate(token: str, key: str, secret: str, code: str) -> list[dict]:
    """입력 차수별 누계 추정치 (오래된 차수 → 최신 차수 순)"""
    data = _get(token, key, secret, "investor-trend-estimate", "HHPTJ04160200", {"MKSC_SHRN_ISCD": code})
    rows = []
    for o in data.get("output2") or []:
        gb = str(o.get("bsop_hour_gb", "")).strip()
        if not gb:
            continue
        rows.append({
            "gb": gb,
            "frgn": to_int(o.get("frgn_fake_ntby_qty")),
            "orgn": to_int(o.get("orgn_fake_ntby_qty")),
            "sum": to_int(o.get("sum_fake_ntby_qty")),
        })
    rows.sort(key=lambda r: r["gb"])
    return rows


def fetch_price(token: str, key: str, secret: str, code: str) -> tuple[int, float] | None:
    try:
        out = _get(token, key, secret, "inquire-price", "FHKST01010100",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}).get("output") or {}
        price = to_int(out.get("stck_prpr"))
        pct = float(str(out.get("prdy_ctrt") or 0).replace(",", "") or 0)
        return (price, pct) if price else None
    except Exception as e:  # 가격은 부가 정보라 실패해도 수급은 보냄
        print(f"현재가 조회 실패({code}): {e}", file=sys.stderr)
        return None


def icon(v: int) -> str:
    return "🔴" if v > 0 else "🔵" if v < 0 else "⚪"


def block(name_: str, code: str, rows: list[dict], price) -> str:
    head = f"<b>{html.escape(name_)}</b>"
    if price:
        p, pct = price
        head += f"  {p:,}원 ({pct:+.2f}%)"
    if not rows:
        return head + "\n아직 집계된 추정치가 없습니다."
    last = rows[-1]
    lines = [
        head,
        f"{icon(last['frgn'])} 외국인 {man(last['frgn'], True)}만주",
        f"{icon(last['orgn'])} 기관 {man(last['orgn'], True)}만주",
        f"{icon(last['sum'])} 합계 {man(last['sum'], True)}만주",
    ]
    if len(rows) > 1:
        prev = rows[-2]
        d_f, d_o = last["frgn"] - prev["frgn"], last["orgn"] - prev["orgn"]
        lines.append(f"직전 집계 대비: 외국인 {man(d_f, True)} · 기관 {man(d_o, True)}")
        flow = " → ".join(man(r["sum"], True) for r in rows)
        lines.append(f"합계 흐름: {flow}")
    return "\n".join(lines)


def main() -> int:
    now = datetime.now(KST)
    try:
        key, secret = os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]
        token = get_token(key, secret)
    except Exception as e:
        msg = f"⚠️ 장중 수급 알림 실패 ({now:%m/%d %H:%M})\n토큰 발급 오류: {e}"
        print(msg, file=sys.stderr)
        send_telegram(html.escape(msg))
        return 1

    blocks, errors, any_data = [], [], False
    for code, name_ in STOCKS:
        try:
            rows = fetch_estimate(token, key, secret, code)
            any_data = any_data or bool(rows)
            blocks.append(block(name_, code, rows, fetch_price(token, key, secret, code)))
        except Exception as e:
            errors.append(f"⚠️ {name_}({code}): {e}")
            print(errors[-1], file=sys.stderr)

    if not any_data and not errors:
        print("추정치 없음(휴장일 또는 첫 집계 전) — 보내지 않음")
        return 0

    text = "\n\n".join([
        f"⏱ <b>장중 수급 추정</b> · {now:%m/%d %H:%M}",
        *blocks,
        *[html.escape(e) for e in errors],
        "<i>※ 증권사 집계 추정치(개인 미제공). 확정치는 장 마감 후 표로 보내드려요.</i>",
    ])
    send_telegram(text)
    if len(errors) == len(STOCKS):
        drop_token()  # 토큰 문제일 수 있으니 다음 실행 때 새로 발급
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
