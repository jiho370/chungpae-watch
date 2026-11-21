"""떨이 항공권 감시 스크립트.

1단계: Google Travel Explore로 인천/김포 출발 '어디든' 최저가를 훑고 기록한다.
2단계: 기록 대비 크게 싸진 도착지만 Google Flights로 정밀 검사한다
       (수하물 포함 가격, 경유 조건, Google의 평소 가격대 대비 할인율).
3단계: 통과한 건을 Claude가 요약하고 메일로 보낸다.

사용법:
    python monitor.py              # 실제 실행
    python monitor.py --dry-run    # 메일 안 보내고 결과만 출력, 기록도 저장 안 함
    python monitor.py --test-email # 메일 설정 확인용 테스트 메일 발송
    python monitor.py --test-claude # Claude API 인증(페더레이션) 확인
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY_FILE = DATA / "history.json"
ALERTS_FILE = DATA / "alerts.json"
USAGE_FILE = DATA / "usage.json"
BASELINE_FILE = DATA / "baseline.json"

KST = timezone(timedelta(hours=9))
SERPAPI_URL = "https://serpapi.com/search.json"


# ─────────────────────────── 공통 유틸 ───────────────────────────

def today_kst() -> date:
    return datetime.now(KST).date()


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def to_price(value) -> int | None:
    """SerpApi 가격 값(숫자 또는 '₩123,000' 같은 문자열)을 정수로."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def won(n: int | None) -> str:
    return "-" if n is None else f"{n:,}원"


def in_excluded(d: date, ranges: list) -> bool:
    for start, end in ranges or []:
        if date.fromisoformat(start) <= d <= date.fromisoformat(end):
            return True
    return False


# ─────────────────────────── SerpApi ───────────────────────────

class QuotaExceeded(Exception):
    pass


class Serp:
    def __init__(self, api_key: str, monthly_limit: int, usage: dict):
        self.api_key = api_key
        self.limit = monthly_limit
        month = today_kst().strftime("%Y-%m")
        if usage.get("month") != month:
            usage.clear()
            usage.update({"month": month, "count": 0})
        self.usage = usage

    def remaining(self) -> int:
        return self.limit - self.usage["count"]

    def search(self, params: dict) -> dict:
        if self.remaining() <= 0:
            raise QuotaExceeded(f"이번 달 검색 한도({self.limit}회)에 도달했습니다.")
        query = {"api_key": self.api_key, "currency": "KRW", "hl": "en", "gl": "kr", **params}
        resp = requests.get(SERPAPI_URL, params=query, timeout=60)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or "error" in data:
            hint = ""
            if resp.status_code == 401:
                # 키 값은 출력하지 않고 길이·형식만 알려준다 (정상 키는 64자리 소문자 16진수)
                k = self.api_key
                is_hex64 = len(k) == 64 and all(c in "0123456789abcdef" for c in k)
                hint = (f" [키 진단: 길이 {len(k)}자, 64자리 16진수 형식={'예' if is_hex64 else '아니오'}, "
                        f"Anthropic 키 형식(sk-ant)={'예' if k.startswith('sk-ant') else '아니오'}]")
            raise RuntimeError(f"SerpApi 오류 ({resp.status_code}): {data.get('error', resp.text[:200])}{hint}")
        self.usage["count"] += 1
        return data


# ─────────────────────────── 1단계: Explore ───────────────────────────

def run_explore(serp: Serp, cfg: dict, history: dict, today: date) -> None:
    """도착지별 오늘의 최저가를 기록한다."""
    dests = history.setdefault("destinations", {})
    today_s = today.isoformat()
    departures = cfg.get("explore_departures") or [cfg["origins"]]
    for duration in cfg["explore_durations"]:
        data = {}
        for dep in departures:  # 앞에서부터 시도해서 결과가 나오는 첫 출발지를 쓴다
            try:
                data = serp.search({
                    "engine": "google_travel_explore",
                    "departure_id": dep,
                    "travel_duration": duration,
                    "month": 0,
                })
            except RuntimeError as exc:
                if "Empty results" in str(exc):
                    print(f"Explore 결과 없음 (출발지 {dep}, 기간 {duration}) → 다음 후보로")
                    continue
                raise
            print(f"Explore 출발지 {dep}, 기간 {duration}: 도착지 {len(data.get('destinations', []))}곳")
            break
        for item in data.get("destinations", []):
            code = (item.get("destination_airport") or {}).get("code")
            price = to_price(item.get("flight_price"))
            start = item.get("start_date")
            if not code or not price or not start:
                continue
            try:
                start_d = date.fromisoformat(start[:10])
            except ValueError:
                continue
            if start_d <= today or in_excluded(start_d, cfg.get("exclude_date_ranges")):
                continue
            entry = dests.setdefault(code, {"prices": []})
            entry["name"] = item.get("name") or entry.get("name") or code
            entry["country"] = item.get("country") or entry.get("country", "")
            entry["prices"].append({"d": today_s, "p": price, "start": start_d.isoformat(), "dur": duration})
            entry["prices"] = entry["prices"][-200:]


def baseline_ref(baseline: dict, code: str, start_iso: str) -> dict | None:
    """미리 모아 둔 평소 가격(Google Flights 달력)에서 도착지·출발 월의 기준값을 찾는다."""
    cities = baseline.get("cities", {})
    city = cities.get(code) or cities.get(baseline.get("aliases", {}).get(code, ""))
    if not city:
        return None
    month = city["months"].get(start_iso[:7])
    if month and month["days"] >= 10:
        return {"min": month["min"], "p25": month["p25"], "scope": start_iso[:7]}
    # 그 달 자료가 없으면 월별 값들의 중앙값으로 대신한다
    solid = [m for m in city["months"].values() if m["days"] >= 10] or list(city["months"].values())
    return {
        "min": int(statistics.median(m["min"] for m in solid)),
        "p25": int(statistics.median(m["p25"] for m in solid)),
        "scope": "월 자료 없음",
    }


def pick_candidates(cfg: dict, history: dict, today: date, baseline: dict | None = None) -> list[dict]:
    """오늘 가격이 평소보다 충분히 싼 도착지를 고른다.

    자체 기록이 min_history_points일 이상 쌓인 도착지는 기록 중앙값과,
    아직 기록이 적은 도착지는 미리 모아 둔 평소 가격(baseline.json)과 비교한다.
    """
    ccfg = cfg["candidate"]
    base_drop = ccfg.get("baseline_drop_ratio", 0.05)
    baseline = baseline or {}
    today_s = today.isoformat()
    cooldown = timedelta(days=cfg["recheck_cooldown_days"])
    out = []
    for code, entry in history.get("destinations", {}).items():
        todays = [p for p in entry["prices"] if p["d"] == today_s]
        if not todays:
            continue
        best_today = min(todays, key=lambda p: p["p"])
        # 이전 날짜별 최저가
        daily: dict[str, int] = {}
        for p in entry["prices"]:
            if p["d"] != today_s:
                daily[p["d"]] = min(daily.get(p["d"], p["p"]), p["p"])
        ref = baseline_ref(baseline, code, best_today["start"])
        if len(daily) >= ccfg["min_history_points"]:
            median = statistics.median(daily.values())
            need, source = ccfg["min_drop_ratio"], "기록"
        elif ref:
            # Explore 가격은 여러 날짜 중 최저가라, 달력에서도 그 달의 최저가와 비교해야 공정하다.
            median = ref["min"]
            # 그 달 자료가 없어 다른 달 값으로 대신할 때는 더 크게 떨어져야 후보로 본다
            need = base_drop if ref["scope"] != "월 자료 없음" else ccfg.get("baseline_fallback_drop_ratio", 0.20)
            source = f"평소 달력 월 최저가({ref['scope']})"
        else:
            continue
        drop = (median - best_today["p"]) / median
        if drop < need:
            continue
        last = entry.get("last_checked")
        if last and today - date.fromisoformat(last) < cooldown:
            continue
        out.append({
            "code": code, "name": entry.get("name", code), "country": entry.get("country", ""),
            "explore_price": best_today["p"], "median": int(median), "drop": drop,
            "start": best_today["start"], "ref_source": source,
            "baseline_p25": ref["p25"] if ref else None,
        })
    out.sort(key=lambda c: c["drop"], reverse=True)
    return out[: cfg["max_candidates_per_run"]]


# ─────────────────────────── 2단계: 정밀 검사 ───────────────────────────

def layovers_ok(itin: dict, cfg: dict) -> bool:
    short_max = cfg["layover"]["short_max_minutes"]
    long_min = cfg["layover"]["long_min_minutes"]
    for lay in itin.get("layovers", []) or []:
        dur = lay.get("duration")
        if dur is None:
            return False
        if not (dur <= short_max or dur >= long_min):
            return False
    return True


def baggage_fee(itin: dict, cfg: dict) -> tuple[int, bool]:
    """(왕복 위탁수하물 추정 요금, 추정 여부). 0이면 포함으로 판단."""
    bcfg = cfg["baggage"]
    table = bcfg["airline_fee_per_leg"]
    texts = list(itin.get("extensions", []) or [])
    for f in itin.get("flights", []) or []:
        texts += f.get("extensions", []) or []
    ext = " ".join(texts).lower()
    airlines = {f.get("airline", "") for f in itin.get("flights", []) or []}

    trusted = [t.lower() for t in bcfg.get("included_unless_flagged", [])]

    def fee_for(airline: str) -> int | None:
        """Google은 'Peach Aviation', 'ZIPAIR Tokyo'처럼 길게 보내므로 표의 이름이 포함되면 일치로 본다."""
        name = airline.lower()
        hits = [(len(k), v) for k, v in table.items() if k.lower() in name]
        return max(hits)[1] if hits else None  # 가장 긴(구체적인) 이름 우선

    def is_trusted(airline: str) -> bool:
        return any(t in airline.lower() for t in trusted)

    # 1) 수하물 포함 표시
    if "checked bag" in ext and ("included" in ext or "free" in ext) and "fee" not in ext \
            and "not included" not in ext:
        return 0, False
    # 2) 유료 표시 → 항공사별 요금
    if "checked bag" in ext and ("fee" in ext or "not included" in ext):
        fees = [f for a in airlines if (f := fee_for(a)) is not None]
        per_leg = max(fees) if fees else bcfg["default_fee_per_leg"]
        return per_leg * 2, True  # 왕복이므로 2회
    # 3) 표시 없음 → 해외 LCC만 요금 추가
    foreign_fees = [f for a in airlines if not is_trusted(a) and (f := fee_for(a)) is not None]
    if foreign_fees:
        return max(foreign_fees) * 2, True
    return 0, False


def flights_link(origin: str, dest: str, out: str, ret: str) -> str:
    q = quote(f"Flights to {dest} from {origin} on {out} through {ret}")
    return f"https://www.google.com/travel/flights?q={q}&curr=KRW&hl=ko"


def check_candidate(serp: Serp, cfg: dict, cand: dict) -> list[dict]:
    """후보 도착지를 기간별로 검색해 조건을 통과한 최선의 일정을 돌려준다."""
    start = date.fromisoformat(cand["start"])
    results = []
    for n in cfg["trip_lengths_days"]:
        ret = start + timedelta(days=n)
        data = serp.search({
            "engine": "google_flights",
            "departure_id": cfg["origins"],
            "arrival_id": cand["code"],
            "outbound_date": start.isoformat(),
            "return_date": ret.isoformat(),
            "type": 1,
        })
        insights = data.get("price_insights", {}) or {}
        typical = insights.get("typical_price_range") or []
        typical_low = to_price(typical[0]) if typical else None
        typical_source = "Google"
        if not typical_low and cand.get("baseline_p25"):
            # Google이 평소 가격대를 안 주면 미리 모아 둔 달력 가격의 하위 25%로 대신 비교
            typical_low, typical_source = cand["baseline_p25"], "평소 달력"

        best = None
        for itin in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            price = to_price(itin.get("price"))
            if not price or not layovers_ok(itin, cfg):
                continue
            fee, estimated = baggage_fee(itin, cfg)
            total = price + fee
            if best is None or total < best["total"]:
                flights = itin.get("flights", []) or []
                best = {
                    "fare": price, "bag_fee": fee, "bag_estimated": estimated, "total": total,
                    "airlines": sorted({f.get("airline", "") for f in flights}),
                    "stops": max(len(flights) - 1, 0),
                    "layovers": [
                        {"airport": l.get("name", l.get("id", "")), "minutes": l.get("duration"),
                         "overnight": bool(l.get("overnight"))}
                        for l in itin.get("layovers", []) or []
                    ],
                    "duration_min": itin.get("total_duration"),
                    "origin": (flights[0].get("departure_airport") or {}).get("id", "ICN") if flights else "ICN",
                }
        if best is None:
            continue

        discount = (typical_low - best["total"]) / typical_low if typical_low else None
        best.update({
            "dest_code": cand["code"], "dest_name": cand["name"], "country": cand["country"],
            "outbound": start.isoformat(), "return": ret.isoformat(), "nights": n,
            "price_level": insights.get("price_level"),
            "typical_range": [to_price(x) for x in typical] if typical else None,
            "discount": discount, "typical_source": typical_source if typical_low else None,
            "link": flights_link(best["origin"], cand["code"], start.isoformat(), ret.isoformat()),
        })
        results.append(best)
    return results


def classify(deal: dict, cfg: dict) -> str | None:
    d = deal["discount"]
    if d is not None:
        if d >= cfg["deal"]["hot_ratio"]:
            return "hot"
        if d >= cfg["deal"]["good_ratio"]:
            return "good"
        return None
    # 평소 가격대 정보가 없을 때는 Google 판정만 사용
    return "good" if deal.get("price_level") == "low" else None


def should_alert(deal: dict, alerts: dict, cfg: dict) -> bool:
    key = f"{deal['dest_code']}|{deal['outbound']}|{deal['return']}"
    prev = alerts.get(key)
    if prev is None:
        return True
    return deal["total"] <= prev["total"] * (1 - cfg["deal"]["realert_drop_ratio"])


def remember_alert(deal: dict, alerts: dict, today: date) -> None:
    key = f"{deal['dest_code']}|{deal['outbound']}|{deal['return']}"
    alerts[key] = {"total": deal["total"], "d": today.isoformat()}


def prune_alerts(alerts: dict, today: date) -> None:
    for key in list(alerts):
        outbound = key.split("|")[1]
        if date.fromisoformat(outbound) < today:
            del alerts[key]


# ─────────────────────────── 3단계: 메일 ───────────────────────────

def deal_lines(deal: dict) -> str:
    tier = "🔥 떨이" if deal["tier"] == "hot" else "👀 괜찮음"
    disc = f"{deal['discount'] * 100:.0f}% 저렴" if deal["discount"] is not None else "Google 판정 '낮음'"
    typical = deal["typical_range"]
    typical_s = f"{won(typical[0])} ~ {won(typical[1])}" if typical and len(typical) >= 2 else "정보 없음"
    if deal["layovers"]:
        lay = ", ".join(
            f"{l['airport']} {l['minutes'] // 60}시간 {l['minutes'] % 60}분" for l in deal["layovers"]
        )
        route = f"경유 {deal['stops']}회 ({lay})"
    else:
        route = "직항"
    bag = (f"운임 {won(deal['fare'])} + 수하물 {won(deal['bag_fee'])}"
           + (" (추정)" if deal["bag_estimated"] else "")) if deal["bag_fee"] else "수하물 포함 운임"
    return (
        f"{tier} | {deal['dest_name']} ({deal['dest_code']}, {deal['country']})\n"
        f"  {deal['origin']} 출발 {deal['outbound']} → 귀국 {deal['return']} ({deal['nights']}일 뒤)\n"
        f"  수하물 포함 {won(deal['total'])}  [{bag}]\n"
        f"  평소 가격대 {typical_s} 대비 {disc}\n"
        f"  {', '.join(deal['airlines'])} · {route}\n"
        f"  {deal['link']}\n"
    )


SUMMARY_SYSTEM = (
    "너는 항공권 특가 알림 메일의 첫머리를 쓰는 비서다. 입력 JSON에 있는 사실만 쓰고, 없는 정보는 지어내지 마라. "
    "한국어로, 3~6줄의 평문으로 쓴다(마크다운·이모지 표 금지). 가장 눈에 띄는 건을 먼저 짚고, "
    "가격은 수하물 포함 가격(total) 기준으로 말한다. bag_estimated가 true면 수하물 요금이 추정치라고 짧게 밝힌다. "
    "24시간 이상 경유가 있으면 '경유지 여행 가능'처럼 장점으로 언급해도 된다."
)


def refresh_github_oidc_token() -> None:
    """GitHub Actions에서 페더레이션용 OIDC 토큰을 받아 파일에 쓴다.

    GitHub 토큰은 약 5분 뒤 만료되므로 Claude를 부르기 직전에 받는다.
    Anthropic SDK가 ANTHROPIC_IDENTITY_TOKEN_FILE을 읽어 단기 접근 토큰으로 교환한다.
    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    token_file = os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE")
    if not (url and req_token and token_file):
        return
    resp = requests.get(
        url,
        params={"audience": "https://api.anthropic.com"},
        headers={"Authorization": f"Bearer {req_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    Path(token_file).write_text(resp.json()["value"], encoding="utf-8")


def claude_summary(deals: list[dict], cfg: dict) -> str | None:
    has_credential = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_FEDERATION_RULE_ID")
    if not cfg["claude"].get("enabled") or not has_credential:
        return None
    try:
        import anthropic

        refresh_github_oidc_token()
        client = anthropic.Anthropic()
        slim = [{k: d[k] for k in ("tier", "dest_name", "country", "outbound", "return", "nights", "total",
                                   "fare", "bag_fee", "bag_estimated", "discount", "typical_range",
                                   "airlines", "stops", "layovers")} for d in deals]
        response = client.messages.create(
            model=cfg["claude"]["model"],
            max_tokens=2000,
            output_config={"effort": "low"},
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(slim, ensure_ascii=False)}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
    except Exception as exc:  # 요약 실패해도 알림은 보낸다
        print(f"[경고] Claude 요약 실패: {exc}", file=sys.stderr)
        return None


def build_email(deals: list[dict], summary: str | None, sample: bool = False) -> tuple[str, str, str]:
    hot = [d for d in deals if d["tier"] == "hot"]
    good = [d for d in deals if d["tier"] == "good"]
    parts = []
    if hot:
        parts.append(f"🔥 {len(hot)}건")
    if good:
        parts.append(f"👀 {len(good)}건")
    subject = f"[떨이 항공권] {' · '.join(parts)} - {deals[0]['dest_name']} {won(deals[0]['total'])}부터"
    body = []
    if summary:
        body += [summary, "", "─" * 40, ""]
    # 도착지별로 묶어서 가장 싼 일정은 자세히, 나머지 기간은 한 줄씩
    groups: dict[str, list[dict]] = {}
    for d in hot + good:
        groups.setdefault(d["dest_code"], []).append(d)
    for items in groups.values():
        items.sort(key=lambda d: d["total"])
        body.append(deal_lines(items[0]).rstrip("\n"))
        if len(items) > 1:
            body.append("  다른 기간:")
            for d in sorted(items[1:], key=lambda d: d["nights"]):
                disc = f", {d['discount'] * 100:.0f}% 저렴" if d["discount"] is not None else ""
                mark = "🔥" if d["tier"] == "hot" else "👀"
                body.append(f"   {mark} {d['outbound']}~{d['return']} ({d['nights']}일): {won(d['total'])}{disc}")
                body.append(f"      {d['link']}")
        body.append("")
    body.append("※ 수하물 요금 '(추정)'은 항공사별 대략적인 금액입니다. 예약 전 실제 요금을 확인하세요.")
    body.append("※ 경유 조건은 가는 편 기준으로 확인했습니다. 오는 편은 링크에서 확인하세요.")
    return subject, "\n".join(body), build_email_html(deals, summary, sample)


WEEKDAYS = "월화수목금토일"


def _md(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def _route_text(deal: dict) -> str:
    if not deal["layovers"]:
        return "직항"
    parts = []
    for l in deal["layovers"]:
        m = l["minutes"] or 0
        rest = f" {m % 60}분" if m % 60 else ""
        parts.append(f"{l['airport']} {m // 60}시간{rest}")
    return f"경유 {deal['stops']}회 · " + ", ".join(parts)


def build_email_html(deals: list[dict], summary: str | None, sample: bool = False) -> str:
    """Gmail에서 깨지지 않도록 표 + 인라인 스타일로만 만든 HTML 메일."""
    font = "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',sans-serif"
    ink, sub, line, bg = "#1f2328", "#6b7280", "#e5e7eb", "#f4f5f7"
    tier_style = {
        "hot": ("떨이", "#b42318", "#fef3f2"),
        "good": ("괜찮음", "#175cd3", "#eff8ff"),
    }

    groups: dict[str, list[dict]] = {}
    for d in sorted(deals, key=lambda d: (d["tier"] != "hot", d["total"])):
        groups.setdefault(d["dest_code"], []).append(d)

    def row(k: str, v: str) -> str:
        return (f'<tr><td style="padding:3px 12px 3px 0;color:{sub};white-space:nowrap;vertical-align:top;">{k}</td>'
                f'<td style="padding:3px 0;color:{ink};">{v}</td></tr>')

    cards = []
    for items in groups.values():
        items.sort(key=lambda d: d["total"])
        top = items[0]
        label, color, tint = tier_style.get(top["tier"], tier_style["good"])
        if top["discount"] is not None:
            disc = f"평소보다 {top['discount'] * 100:.0f}% 저렴"
        else:
            disc = "Google 판정 '낮음'"
        typical = top["typical_range"]
        if typical and len(typical) >= 2:
            typical_s = f"{won(typical[0])} ~ {won(typical[1])}"
        else:
            typical_s = "정보 없음"
        if top.get("typical_source") == "평소 달력":
            typical_s += " (달력 기준)"
        if top["bag_fee"]:
            est = " (추정)" if top["bag_estimated"] else ""
            bag = f"운임 {won(top['fare'])} + 수하물 {won(top['bag_fee'])}{est}"
        else:
            bag = "수하물 포함 운임"

        others = ""
        if len(items) > 1:
            rows = []
            for d in sorted(items[1:], key=lambda d: d["nights"]):
                dd = f"{d['discount'] * 100:.0f}%↓" if d["discount"] is not None else ""
                rows.append(
                    f'<tr><td style="padding:7px 0;border-top:1px solid {line};color:{ink};">'
                    f'{_md(d["outbound"])} → {_md(d["return"])} · {d["nights"]}박</td>'
                    f'<td style="padding:7px 0;border-top:1px solid {line};text-align:right;white-space:nowrap;">'
                    f'<a href="{escape(d["link"])}" style="color:{ink};text-decoration:none;font-weight:600;">'
                    f'{won(d["total"])}</a> <span style="color:{color};font-size:12px;">{dd}</span></td></tr>')
            others = (f'<div style="margin-top:16px;font-size:12px;color:{sub};">다른 기간</div>'
                      f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                      f'style="font-size:13px;margin-top:4px;">{"".join(rows)}</table>')

        cards.append(f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border:1px solid {line};border-radius:12px;margin:0 0 16px;">
<tr><td style="padding:20px 22px;">
  <span style="display:inline-block;padding:3px 9px;border-radius:999px;background:{tint};color:{color};font-size:12px;font-weight:700;">{label}</span>
  <span style="font-size:12px;color:{sub};margin-left:6px;">{escape(top['country'])}</span>
  <div style="font-size:20px;font-weight:700;color:{ink};margin:8px 0 2px;">{escape(top['dest_name'])} <span style="font-size:14px;color:{sub};font-weight:400;">{top['dest_code']}</span></div>
  <div style="font-size:14px;color:{sub};">{top['origin']} 출발 · {_md(top['outbound'])} → {_md(top['return'])} · {top['nights']}박</div>
  <div style="margin:14px 0 2px;"><span style="font-size:28px;font-weight:800;color:{ink};letter-spacing:-0.5px;">{won(top['total'])}</span><span style="font-size:13px;color:{sub};margin-left:6px;">수하물 포함</span></div>
  <div style="font-size:14px;font-weight:700;color:{color};margin-bottom:12px;">{disc}</div>
  <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:13px;line-height:1.5;">
    {row("평소", typical_s)}
    {row("요금", bag)}
    {row("항공사", escape(", ".join(top['airlines'])))}
    {row("경로", escape(_route_text(top)))}
  </table>
  {others}
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:18px;"><tr>
    <td style="background:{ink};border-radius:8px;"><a href="{escape(top['link'])}" style="display:inline-block;padding:10px 18px;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none;">Google Flights에서 보기</a></td>
  </tr></table>
</td></tr></table>""")

    summary_html = ""
    if summary:
        paras = "".join(f'<p style="margin:0 0 6px;">{escape(x)}</p>' for x in summary.splitlines() if x.strip())
        summary_html = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                        f'style="margin:0 0 16px;"><tr><td style="padding:14px 18px 8px;background:#ffffff;'
                        f'border-left:3px solid {ink};border-radius:8px;font-size:14px;line-height:1.6;'
                        f'color:{ink};">{paras}</td></tr></table>')

    sample_html = ""
    if sample:
        sample_html = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                       f'style="margin:0 0 16px;"><tr><td style="padding:12px 16px;background:#fffaeb;'
                       f'border:1px solid #fedf89;border-radius:8px;font-size:14px;line-height:1.5;color:#93370d;">'
                       f'<b>견본 메일</b> · 레이아웃 확인용으로 만든 가짜 데이터예요. '
                       f'가격·날짜는 실제가 아니라서 링크를 눌러도 같은 가격이 나오지 않아요.</td></tr></table>')
    hot_n = sum(1 for d in deals if d["tier"] == "hot")
    head = f"오늘 찾은 항공권 {len(groups)}곳" + (f" · 떨이 {hot_n}건" if hot_n else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{bg};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{bg};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;font-family:{font};">
<tr><td style="padding:0 4px 14px;">
  <div style="font-size:12px;color:{sub};">떨이 항공권 · 인천/김포 출발</div>
  <div style="font-size:22px;font-weight:800;color:{ink};margin-top:2px;">{head}</div>
</td></tr>
<tr><td>{sample_html}{summary_html}{"".join(cards)}</td></tr>
<tr><td style="padding:4px 4px 0;font-size:12px;line-height:1.6;color:{sub};">
  수하물 '(추정)'은 항공사별 대략적인 금액이라 예약 전 실제 요금을 확인하세요.<br>
  경유 조건(6시간 이하 또는 24시간 이상)은 가는 편만 확인했습니다.
</td></tr>
</table></td></tr></table></body></html>"""


SAMPLE_DEALS = [
    {"tier": "hot", "dest_code": "KIX", "dest_name": "Osaka", "country": "Japan", "origin": "ICN",
     "outbound": "2027-04-29", "return": "2027-05-03", "nights": 4, "fare": 200777, "bag_fee": 0,
     "bag_estimated": False, "total": 200777, "discount": 0.21, "typical_range": [255000, 400000],
     "typical_source": "Google", "price_level": "low", "airlines": ["Jeju Air"], "stops": 0, "layovers": [],
     "link": "https://www.google.com/travel/flights?q=Flights%20to%20KIX%20from%20ICN%20on%202027-04-29%20through%202027-05-03"},
    {"tier": "good", "dest_code": "KIX", "dest_name": "Osaka", "country": "Japan", "origin": "ICN",
     "outbound": "2027-04-29", "return": "2027-05-05", "nights": 6, "fare": 180000, "bag_fee": 90000,
     "bag_estimated": True, "total": 270000, "discount": 0.12, "typical_range": [300000, 450000],
     "typical_source": "Google", "price_level": "low", "airlines": ["Peach Aviation"], "stops": 0, "layovers": [],
     "link": "https://www.google.com/travel/flights?q=Flights%20to%20KIX%20from%20ICN%20on%202027-04-29%20through%202027-05-05"},
    {"tier": "good", "dest_code": "HIJ", "dest_name": "Hiroshima", "country": "Japan", "origin": "ICN",
     "outbound": "2027-01-28", "return": "2027-02-01", "nights": 4, "fare": 226700, "bag_fee": 0,
     "bag_estimated": False, "total": 226700, "discount": 0.14, "typical_range": [265000, 475000],
     "typical_source": "Google", "price_level": "low", "airlines": ["Jeju Air"], "stops": 1,
     "layovers": [{"airport": "Fukuoka Airport", "minutes": 135, "overnight": False}],
     "link": "https://www.google.com/travel/flights?q=Flights%20to%20HIJ%20from%20ICN%20on%202027-01-28%20through%202027-02-01"},
]


def send_email(subject: str, body: str, html: str | None = None) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("MAIL_TO") or user
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


# ─────────────────────────── 메인 ───────────────────────────

def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔에서도 한글·이모지가 깨지지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--test-claude", action="store_true")
    parser.add_argument("--insights", type=int, default=0, metavar="N",
                        help="가장 싼 도착지 N곳의 평소 가격대 비교 (검색 N회 소모)")
    args = parser.parse_args()

    if args.test_claude:
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        import anthropic

        refresh_github_oidc_token()
        response = anthropic.Anthropic().messages.create(
            model=cfg["claude"]["model"],
            max_tokens=200,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "연결 테스트입니다. '연결 성공'이라고만 답하세요."}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        print(f"Claude 응답: {text} (stop_reason={response.stop_reason})")
        return 0

    if args.test_email:
        _, body, html = build_email(SAMPLE_DEALS, None, sample=True)
        body = "[견본] 아래 가격·날짜는 레이아웃 확인용 가짜 데이터입니다.\n\n" + body
        send_email("[떨이 항공권] 테스트 메일 (견본)", body, html)
        print("테스트 메일을 보냈습니다.")
        return 0

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    history = load_json(HISTORY_FILE, {"destinations": {}})
    alerts = load_json(ALERTS_FILE, {})
    usage = load_json(USAGE_FILE, {})
    today = today_kst()
    serp = Serp(os.environ["SERPAPI_KEY"].strip(), cfg["monthly_search_limit"], usage)

    if args.insights:
        # 가장 최근 기록에서 싼 도착지 N곳을 Google Flights로 조회해 평소 가격대와 비교
        rows = []
        for code, entry in history.get("destinations", {}).items():
            if not entry["prices"]:
                continue
            last_d = max(p["d"] for p in entry["prices"])
            best = min((p for p in entry["prices"] if p["d"] == last_d), key=lambda p: p["p"])
            rows.append((best["p"], code, entry, best))
        rows.sort(key=lambda r: r[0])
        for explore_price, code, entry, best in rows[: args.insights]:
            nights = 2 if best["dur"] == 1 else 7  # Explore 기간: 주말 → 2박, 1주 → 7박
            cand = {"code": code, "name": entry.get("name", code), "country": entry.get("country", ""),
                    "start": best["start"]}
            try:
                results = check_candidate(serp, {**cfg, "trip_lengths_days": [nights]}, cand)
            except (RuntimeError, QuotaExceeded) as exc:
                print(f"{cand['name']} ({code}): 조회 실패 - {exc}")
                continue
            if not results:
                print(f"{cand['name']} ({code}) {best['start']}: 경유 조건을 통과한 항공편 없음")
                continue
            d = results[0]
            typical = d["typical_range"]
            typical_s = f"{won(typical[0])}~{won(typical[1])}" if typical and len(typical) >= 2 else "정보 없음"
            if d["discount"] is None:
                disc = "-"
            elif d["discount"] >= 0:
                disc = f"{d['discount'] * 100:.0f}% 저렴"
            else:
                disc = f"{-d['discount'] * 100:.0f}% 비쌈"
            print(f"{d['dest_name']} ({code}) {d['outbound']}~{d['return']} | Explore {won(explore_price)} | "
                  f"수하물 포함 {won(d['total'])}{' (추정 포함)' if d['bag_estimated'] else ''} | "
                  f"Google 판정 {d['price_level']} | 평소 {typical_s} | 평소 하한 대비 {disc} | "
                  f"{', '.join(d['airlines'])}")
        print(f"이번 달 SerpApi 사용: {usage['count']}/{cfg['monthly_search_limit']}회")
        save_json(USAGE_FILE, usage)
        return 0

    deals: list[dict] = []
    try:
        run_explore(serp, cfg, history, today)
        candidates = pick_candidates(cfg, history, today, load_json(BASELINE_FILE, {}))
        print(f"후보 {len(candidates)}개: " + ", ".join(
            f"{c['code']} {won(c['explore_price'])} (기준 {won(c['median'])}, {c['ref_source']}, -{c['drop'] * 100:.0f}%)"
            for c in candidates))
        for cand in candidates:
            if serp.remaining() < len(cfg["trip_lengths_days"]):
                print("남은 검색 횟수가 부족해 정밀 검사를 건너뜁니다.")
                break
            history["destinations"][cand["code"]]["last_checked"] = today.isoformat()
            for deal in check_candidate(serp, cfg, cand):
                tier = classify(deal, cfg)
                if tier and should_alert(deal, alerts, cfg):
                    deal["tier"] = tier
                    deals.append(deal)
    except QuotaExceeded as exc:
        print(exc)

    print(f"이번 달 SerpApi 사용: {usage['count']}/{cfg['monthly_search_limit']}회")

    if deals:
        deals.sort(key=lambda d: (d["tier"] != "hot", d["total"]))
        subject, body, html = build_email(deals, claude_summary(deals, cfg))
        if args.dry_run:
            print(f"\n제목: {subject}\n\n{body}")
        else:
            send_email(subject, body, html)
            for d in deals:
                remember_alert(d, alerts, today)
            print(f"메일 발송: {subject}")
    else:
        print("알릴 만한 떨이가 없습니다.")

    if not args.dry_run:
        prune_alerts(alerts, today)
        save_json(HISTORY_FILE, history)
        save_json(ALERTS_FILE, alerts)
        save_json(USAGE_FILE, usage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
