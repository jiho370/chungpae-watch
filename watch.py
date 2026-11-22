"""청패 공연 감시 + 지메일 알림.

    python watch.py               한 번 실행(5분마다 스케줄러가 부름)
    python watch.py --dry-run     메일 대신 화면에 출력, 상태 저장 안 함
    python watch.py --test-mail   테스트 메일 한 통 보내기
    python watch.py --force-lists 주기와 상관없이 목록도 확인

예매·결제는 하지 않습니다. 알림만 보냅니다.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import tomllib
from datetime import date, datetime, timedelta, timezone

from cpw import filters, net, notify, sources, state as statemod
from cpw.net import FetchError, ParseError

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
WEEK = "월화수목금토일"


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def kst_ts(s: str) -> float:
    """'2026-11-25 17:00:00' (KST) → epoch."""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()


def fmt_date(d: str, t: str = "") -> str:
    try:
        wd = WEEK[date.fromisoformat(d).weekday()]
        return f"{d}({wd})" + (f" {t}" if t else "")
    except ValueError:
        return d


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_business(dt: datetime, sc: dict) -> bool:
    if WEEK[dt.weekday()] not in sc.get("business_days", list(WEEK)):
        return False
    start, end = (_hm(x) for x in sc["business_hours"])
    return start <= dt.hour * 60 + dt.minute < end


def soldout_interval(dt: datetime, sc: dict) -> int:
    t = dt.hour * 60 + dt.minute
    for w in sc.get("soldout_intervals", []):
        a, b = _hm(w["from"]), _hm(w["to"])
        if (a <= t < b) if a <= b else (t >= a or t < b):
            return int(w["every_min"])
    return int(sc["soldout_default_min"])


class Run:
    def __init__(self, cfg, st, now):
        self.cfg, self.st, self.now = cfg, st, now
        self.dt = datetime.fromtimestamp(now, KST)
        self.today = self.dt.date().isoformat()
        self.business = in_business(self.dt, cfg["schedule"])
        self.err: dict[str, str] = {}
        self.ok: set[str] = set()
        self.new: list[str] = []          # 새로 대상이 된 상품코드
        self.cp_added: list[str] = []     # 청패에 새로 올라온 (이미 아는) 대상 공연
        self.seat_alerts: list[dict] = []
        self.status: dict[str, list] = {}  # 상품코드 → 이번 실행의 저가석 현황

    def guard(self, key, fn, *a):
        try:
            r = fn(*a)
            self.ok.add(key)
            return r
        except (FetchError, ParseError) as e:
            self.err.setdefault(key, f"{type(e).__name__}: {e}")
            return None

    # ── NOL 상품 캐시 ──
    def product(self, code: str):
        c = self.st["products"].get(code)
        if c and self.now - c.get("fetched", 0) < 24 * 3600:
            return c
        p = self.guard("NOL 상품", sources.nol_product, code)
        if p is None:
            return c  # 실패하면 예전 값이라도 사용
        p["fetched"] = self.now
        if c and "sched" in c:
            p["sched"], p["sched_at"] = c["sched"], c.get("sched_at", 0)
        self.st["products"][code] = p
        return p

    # ── 1. 목록 ──
    def lists(self):
        cfg, st = self.cfg, self.st
        perfs = []
        for g in cfg["chungpae"]["genres"]:
            r = self.guard("청패 목록", sources.chungpae_list, g)
            perfs += r or []
        sp = self.guard("서울시향 목록", sources.seoulphil_list) or []
        cp_codes = {p["goods"] for p in perfs}
        for p in sp:
            if p["goods"] in cp_codes:
                p["chungpae"] = True
        # 청패 항목을 먼저 처리(제목·청패 여부가 더 정확)
        for p in perfs + sp:
            self.consider(p)
        for code in cfg["watch"].get("extra_goods", []):
            code = str(code)
            old = st["goods"].get(code) or {}
            if old.get("tier") == "직접 지정":
                continue
            prod = self.product(code)
            if not prod:
                continue
            cap = cfg["watch"].get("extra_max_price", 0)
            cheapest = not cap  # 0이면 그 공연의 가장 싼 등급만 (가격 상한 없음)
            label = "가장 싼 등급" if cheapest else f"{cap:,}원 이하"
            cap = cap or 10 ** 9
            st["goods"][code] = {
                "title": old.get("title") or prod["title"] or code, "venue": old.get("venue") or prod["venue"],
                "start": old.get("start", ""), "end": old.get("end", ""), "time": old.get("time", ""),
                "registered": old.get("registered", ""), "pinned": old.get("pinned"),
                "chungpae": old.get("chungpae") or code in cp_codes, "qualified": True, "tier": "직접 지정",
                "reason": f"config의 extra_goods, {label}", "max_price": cap, "cheapest_only": cheapest,
                "link": sources.nol_url(code), "first_seen": self.now,
            }
        self.popularity()  # 찜 수 갱신 + 보류 공연 재확인 + 유명 공연 추가
        st["last_lists"] = self.now

    def consider(self, p: dict):
        cfg, st = self.cfg, self.st
        code = p["goods"]
        if not code or not p.get("bookable", True):
            return
        if p.get("end") and p["end"] < self.today:
            return
        until = cfg["filter"].get("until", "")
        if until and p.get("start") and p["start"] > until:
            return
        g = st["goods"].get(code)
        if g is not None:
            if p["chungpae"] and not g.get("chungpae"):
                g["chungpae"] = True
                if g.get("qualified") and st["initialized"]:
                    self.cp_added.append(code)
            if p["source"] == "서울시향" and not g.get("sp_link"):
                g["sp_link"] = p["link"]
            if p.get("end", "") > g.get("end", ""):
                g["end"] = p["end"]
            return
        if p["venue"] and not filters.venue_ok(p["venue"], cfg):
            return  # 공연장 밖: 상품 페이지도 열지 않음
        if filters.excluded(p, cfg):
            return
        prod = self.product(code)
        if prod is None:
            return  # 다음 실행에서 다시 시도
        venue = p["venue"] or prod["venue"]
        if not filters.venue_ok(venue, cfg):
            return
        tier, reason = filters.match_tier(p, prod["grades"], cfg)
        g = {
            "title": p["title"], "venue": venue, "start": p["start"], "end": p["end"],
            "time": p.get("time", ""), "chungpae": p["chungpae"], "link": sources.nol_url(code),
            "first_seen": self.now, "qualified": False, "pinned_at": 0,
            "registered": p.get("registered", ""),
        }
        if p["source"] == "서울시향":
            g["sp_link"] = p["link"]
        if tier:
            cheapest = tier.get("cheapest_only", False)
            cheap = filters.cheap_grades(prod["grades"], tier["max_price"], cfg, cheapest)
            price_unknown = not prod["grades"]
            g.update(tier=tier["name"], reason=reason, max_price=tier["max_price"],
                     cheapest_only=cheapest, price_unknown=price_unknown)
            g["qualified"] = bool(cheap) or tier.get("always_notify", False) or price_unknown
            need = tier.get("min_pinned", 0)
            if g["qualified"] and need:
                n = self.guard("NOL 찜 수", sources.nol_pinned, code)
                if n is not None:
                    g["pinned"], g["pinned_at"] = n, self.now
                if (n or 0) < need:
                    # 찜이 모자라면 보류: popularity()가 주기적으로 다시 보고 기준을 넘으면 알림
                    g["qualified"], g["pending_tier"] = False, tier["name"]
                else:
                    g["reason"] += f", 찜 {n}명"
        st["goods"][code] = g
        if g["qualified"] and st["initialized"]:
            self.new.append(code)

    def popularity(self):
        cfg, st = self.cfg, self.st
        pc = cfg["popularity"]
        tier = filters.tier_by_name(cfg, pc.get("tier", "")) or cfg["filter"]["tiers"][-1]
        cheapest = tier.get("cheapest_only", False)
        for code, g in st["goods"].items():
            if g.get("end") and g["end"] < self.today:
                continue
            # 찜 수는 대상 공연도 갱신(메일 정렬용), 미대상 공연은 기준을 넘는지 확인
            if self.now - g.get("pinned_at", 0) < pc["check_interval_hours"] * 3600:
                continue
            n = self.guard("NOL 찜 수", sources.nol_pinned, code)
            if n is None:
                continue
            g["pinned_at"], g["pinned"] = self.now, n
            if g.get("qualified"):
                continue
            pend = filters.tier_by_name(cfg, g.get("pending_tier", ""))
            if pend and n >= pend.get("min_pinned", 0):
                g.update(qualified=True, reason=g.get("reason", "") + f", 찜 {n}명")
                g.pop("pending_tier", None)
                if st["initialized"]:
                    self.new.append(code)
                continue
            if not pc.get("enabled") or n < pc["pinned_threshold"]:
                continue
            prod = self.product(code)
            if prod and filters.cheap_grades(prod["grades"], tier["max_price"], cfg, cheapest):
                g.update(qualified=True, tier=f"{tier['name']}(유명)", reason=f"NOL 찜 {n}명",
                         max_price=tier["max_price"], cheapest_only=cheapest)
                if st["initialized"]:
                    self.new.append(code)

    # ── 2. 잔여석 ──
    def seats(self):
        cfg, st = self.cfg, self.st
        sc = cfg["schedule"]
        last_check, plenty_map = st.setdefault("last_check", {}), st.setdefault("plenty", {})
        so_min = soldout_interval(self.dt, sc)
        for code, g in list(st["goods"].items()):
            if not g.get("qualified") or (g.get("end") and g["end"] < self.today):
                continue
            if st["initialized"] and code not in self.new and code in last_check:  # 첫 판정 때는 전부 확인
                elapsed = (self.now - last_check[code]) / 60
                if plenty_map.get(code):
                    # 넉넉한 공연: 업무시간에만, 업무 주기로
                    if not self.business or elapsed < sc["business_interval_min"] - 5:
                        continue
                elif so_min == 0 or elapsed < so_min - min(so_min / 2, 15):
                    # 매진 공연: 시간대별 주기 (cron 지연을 감안해 여유를 둠)
                    continue
            prod = self.product(code)
            if not prod:
                continue
            cheap = filters.cheap_grades(prod["grades"], g.get("max_price", 0), cfg, g.get("cheapest_only", False))
            if not cheap:
                continue
            if self.now - prod.get("sched_at", 0) > 6 * 3600 or "sched" not in prod:
                end = max(g.get("end") or self.today, self.today)
                end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
                sch = self.guard("NOL 회차", sources.nol_schedules, code, prod["place_code"], self.today, end)
                if sch is None:
                    continue
                prod["sched"], prod["sched_at"] = sch, self.now
            total, any_zero, rows = 0, False, []
            for s in prod["sched"]:
                if s.get("playDate", "") < self.today:
                    continue
                seq = s["playSeq"]
                try:
                    opn, cls = kst_ts(s["saleOpenTime"]), kst_ts(s["saleCloseTime"])
                except (KeyError, ValueError):
                    opn, cls = 0, float("inf")
                if self.now > cls:
                    continue
                if self.now < opn:
                    for sg in cheap:
                        st["seats"].setdefault(f"{code}:{seq}:{sg}", 0)
                        st.setdefault("presale", {})[f"{code}:{seq}"] = True
                    rows.append({"date": s["playDate"], "time": s.get("playTime", ""),
                                 "presale": s["saleOpenTime"][:16]})
                    any_zero = True
                    continue
                rem = self.guard("NOL 잔여석", sources.nol_remaining, code, seq)
                if rem is None:
                    continue
                if rem and all(v == 3000 for v in rem.values()):
                    continue  # 외부 연동 공연: 잔여 수가 실제 값이 아님
                was_presale = st.get("presale", {}).pop(f"{code}:{seq}", False)
                for sg, gr in cheap.items():
                    if sg not in rem:
                        continue
                    cur = rem[sg]
                    key = f"{code}:{seq}:{sg}"
                    prev = st["seats"].get(key)
                    alerted = st.setdefault("alerted", {})
                    info = {"code": code, "date": s["playDate"], "time": s.get("playTime", ""),
                            "grade": gr["name"], "price": gr["price"], "prev": prev, "cur": cur,
                            "open": was_presale, "gone": False}
                    if prev == 0 and cur > 0 and st["initialized"] and code not in self.new:
                        self.seat_alerts.append(info)
                        alerted[key] = self.now
                    elif cur == 0 and key in alerted:
                        # 알려 준 취소표가 다시 사라짐
                        info["gone"] = True
                        self.seat_alerts.append(info)
                        del alerted[key]
                    st["seats"][key] = cur
                    total += cur
                    any_zero |= cur == 0
                    rows.append({"date": s["playDate"], "time": s.get("playTime", ""),
                                 "grade": gr["name"], "price": gr["price"], "cur": cur})
            self.status[code] = rows
            last_check[code] = self.now
            plenty_map[code] = not any_zero and total >= sc["plenty_threshold"]

    # ── 3. 메일 ──
    def ordered(self, codes) -> list:
        """최근 7일 안에 등록된 공연을 먼저, 그 안에서는 찜 많은 순."""
        cut = (self.dt.date() - timedelta(days=7)).isoformat()
        gs = self.st["goods"]
        return sorted(dict.fromkeys(codes), key=lambda c: (
            0 if (gs[c].get("registered") or "") >= cut else 1, -(gs[c].get("pinned") or 0)))

    def describe(self, code: str, head: str) -> str:
        g = self.st["goods"][code]
        prod = self.st["products"].get(code, {})
        lines = [f"■ {head}: {g['title']}"]
        when = fmt_date(g.get("start", ""), g.get("time", ""))
        if g.get("end") and g["end"] != g.get("start"):
            when += f" ~ {g['end']}"
        lines.append(f"  일시: {when}")
        lines.append(f"  장소: {g.get('venue', '')}")
        lines.append(f"  분류: {g.get('tier', '')} ({g.get('reason', '')})")
        extra = []
        if g.get("pinned") is not None:
            extra.append(f"찜 {g['pinned']}명")
        if g.get("registered"):
            extra.append(f"등록 {g['registered']}")
        if extra:
            lines.append("  " + " · ".join(extra))
        grades = filters.usable_grades(prod.get("grades", {}), self.cfg)
        if grades:
            gl = sorted(grades.values(), key=lambda x: -x["price"])
            lines.append("  가격: " + ", ".join(f"{x['name']} {x['price']:,}" for x in gl))
        elif g.get("price_unknown"):
            lines.append("  가격: 확인 필요(페이지에서 가격을 읽지 못함)")
        future_open = ""
        if prod.get("open_time") and prod["open_time"][:4] != "9999":
            try:
                if self.now < kst_ts(prod["open_time"]) < self.now + 180 * 86400:  # 먼 미래 값은 '미정' 표시
                    future_open = prod["open_time"][:16]
                    lines.append(f"  티켓 오픈: {future_open}")
            except ValueError:
                pass
        rows = self.status.get(code)
        if rows:
            label = "가장 싼 등급" if g.get("cheapest_only") else f"{g.get('max_price', 0):,}원 이하"
            lines.append(f"  감시 좌석({label}) 현황:")
            for r in rows:
                if "presale" in r:
                    lines.append(f"    {fmt_date(r['date'], r['time'])}  판매 전 (오픈 {r['presale']})")
                else:
                    st = (f"{r['cur']}석" if r["cur"] else
                          f"0석 (일반 오픈 {future_open} 전, 열리면 알림)" if future_open else
                          "매진 → 취소표 감시 중")
                    lines.append(f"    {fmt_date(r['date'], r['time'])}  {r['grade']} {r['price']:,}원: {st}")
        lines.append(f"  예매(NOL): {g['link']}")
        if g.get("sp_link"):
            lines.append(f"  서울시향 안내: {g['sp_link']}")
        if g.get("chungpae"):
            lines.append(f"  청패 포인트: {sources.CP_PAGE} 에서 '포인트로 예매하기'")
        else:
            lines.append("  청패: 아직 청패 목록에 없음(올라오면 따로 알려 드림)")
        return "\n".join(lines)

    def compose(self):
        st, pre = self.st, self.cfg["mail"]["subject_prefix"]
        parts, subj = [], []
        for a in self.seat_alerts:
            g = st["goods"][a["code"]]
            if a["gone"]:
                parts.append(
                    f"■ 다시 매진: {g['title']}\n"
                    f"  일시: {fmt_date(a['date'], a['time'])}\n"
                    f"  좌석: {a['grade']} {a['price']:,}원 — {a['prev']}석 → 0석\n"
                    f"  앞서 알려 드린 자리가 모두 나갔습니다. 계속 취소표를 감시합니다.\n")
                continue
            head = "티켓 오픈" if a["open"] else "저가석 풀림"
            parts.append(
                f"■ {head}: {g['title']}\n"
                f"  일시: {fmt_date(a['date'], a['time'])}\n"
                f"  장소: {g.get('venue', '')}\n"
                f"  좌석: {a['grade']} {a['price']:,}원 — {a['prev']}석 → {a['cur']}석\n"
                f"  예매(NOL): {g['link']}\n"
                + (f"  청패 포인트: {sources.CP_PAGE} 에서 '포인트로 예매하기'\n" if g.get("chungpae")
                   else "  청패: 아직 청패 목록에 없음\n"))
        opened = [a for a in self.seat_alerts if not a["gone"]]
        gone = [a for a in self.seat_alerts if a["gone"]]
        if opened:
            a = opened[0]
            t = st["goods"][a["code"]]["title"]
            subj.append(f"저가석 풀림 {t} {a['grade']} {a['cur']}석"
                        + (f" 외 {len(opened) - 1}건" if len(opened) > 1 else ""))
        if gone:
            a = gone[0]
            t = st["goods"][a["code"]]["title"]
            subj.append(f"다시 매진 {t} {a['grade']}" + (f" 외 {len(gone) - 1}건" if len(gone) > 1 else ""))
        self.new = self.ordered(self.new)
        for code in self.new:
            parts.append(self.describe(code, "새 공연"))
        if self.new:
            t = st["goods"][self.new[0]]["title"]
            subj.append(f"새 공연 {t}" + (f" 외 {len(set(self.new)) - 1}건" if len(set(self.new)) > 1 else ""))
        for code in dict.fromkeys(self.cp_added):
            parts.append(self.describe(code, "청패에 등록됨(포인트 사용 가능)"))
        if self.cp_added:
            subj.append(f"청패 등록 {st['goods'][self.cp_added[0]]['title']}")
        # 오류: 같은 오류는 한 번만, 복구되면 한 번
        for k, msg in self.err.items():
            if k not in st["errors"]:
                st["errors"][k] = {"since": self.now, "msg": msg}
                parts.append(f"■ 오류({k}): {msg}\n  사이트 구조 변경이나 접속 차단일 수 있습니다. 복구되면 다시 알려 드립니다.")
                subj.append(f"오류 {k}")
        for k in list(st["errors"]):
            if k in self.ok and k not in self.err:
                since = datetime.fromtimestamp(st["errors"][k]["since"], KST).strftime("%m-%d %H:%M")
                parts.append(f"■ 복구됨({k}): {since}부터 이어진 오류가 해결되었습니다.")
                subj.append(f"복구 {k}")
                del st["errors"][k]
        if not parts:
            return None
        return f"{pre} " + " / ".join(subj), "\n\n".join(parts) + "\n"

    def baseline_mail(self, rebaseline=False):
        st, pre = self.st, self.cfg["mail"]["subject_prefix"]
        codes = [c for c, g in st["goods"].items() if g.get("qualified")]
        codes = self.ordered(codes)
        head = "설정 변경을 반영했습니다" if rebaseline else "감시를 시작합니다"
        body = [f"{head}. 지금 조건에 맞는 공연은 {len(codes)}개입니다.",
                "앞으로는 새 공연·저가석 풀림·청패 등록이 생길 때만 메일이 옵니다.", ""]
        body += [self.describe(c, "대상") for c in codes]
        if self.err:
            body.append("■ 첫 실행 중 오류: " + "; ".join(f"{k}: {v}" for k, v in self.err.items()))
            for k, v in self.err.items():
                st["errors"][k] = {"since": self.now, "msg": v}
        title = "설정 변경 반영" if rebaseline else "감시 시작"
        return f"{pre} {title} — 대상 {len(codes)}개", "\n\n".join(body) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    ap.add_argument("--force-lists", action="store_true")
    ap.add_argument("--save", action="store_true", help="--dry-run이어도 상태 저장(테스트용)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--state", default=os.path.join(HERE, "state", "state.json"))
    a = ap.parse_args()

    load_env(os.path.join(HERE, ".env"))
    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    to = cfg["mail"].get("to", "")

    if a.test_mail:
        notify.send(f"{cfg['mail']['subject_prefix']} 테스트 메일",
                    "청패 공연 감시 스크립트의 테스트 메일입니다. 이 메일이 보이면 발송 설정이 끝난 것입니다.", to)
        print("테스트 메일 발송 완료")
        return 0

    net.set_delay(cfg["schedule"]["request_delay_sec"])
    st = statemod.load(a.state)
    st.setdefault("goods", {})
    # 선별 조건(filter/popularity/watch)이 바뀌면 대상을 처음부터 다시 판정하고 목록 메일을 한 번 보냄
    cfg_hash = hashlib.sha1(json.dumps([cfg["filter"], cfg["popularity"], cfg["watch"]],
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    rebaseline = st["initialized"] and st.get("cfg_hash") != cfg_hash
    if rebaseline:
        st["goods"], st["initialized"] = {}, False
    run = Run(cfg, st, time.time())

    lists_due = run.business and \
        run.now - st.get("last_lists", 0) >= (cfg["schedule"]["business_interval_min"] - 5) * 60
    if a.force_lists or not st["initialized"] or lists_due:
        run.lists()
    run.seats()

    # 실행 간격 감시: 5분 주기 시간대인데 직전 실행과 너무 벌어지면 오류 메일(같은 오류는 한 번만)
    sc, last = cfg["schedule"], st.get("last_run", 0)
    if st["initialized"] and last:
        gap = (run.now - last) / 60
        base = sc["soldout_default_min"]
        both_fast = soldout_interval(run.dt, sc) == base and             soldout_interval(datetime.fromtimestamp(last, KST), sc) == base
        if both_fast and sc.get("gap_alert_min", 30) < gap < 360:  # 밤 휴식을 건넌 간격은 제외
            run.err.setdefault("실행 주기", f"직전 실행과 {gap:.0f}분 간격입니다(예정 {base}분). "
                               "Cloudflare 트리거가 멈췄거나 GitHub 토큰이 만료됐을 수 있습니다.")
        else:
            run.ok.add("실행 주기")
    st["last_run"] = run.now

    if not st["initialized"]:
        if not st["goods"]:
            print("첫 실행인데 공연을 하나도 못 읽었습니다:", run.err, file=sys.stderr)
            return 1
        mail = run.baseline_mail(rebaseline)
        st["initialized"], st["cfg_hash"] = True, cfg_hash
    else:
        mail = run.compose()

    if mail:
        subject, body = mail
        if a.dry_run:
            print("=" * 60, "\n제목:", subject, "\n" + body)
        else:
            try:
                notify.send(subject, body, to)
            except Exception as e:  # noqa: BLE001
                # 상태를 저장하지 않고 실패로 끝냄 → 다음 실행에서 같은 알림을 다시 시도
                print("메일 발송 실패:", e, file=sys.stderr)
                return 2
            print("메일 발송:", subject)
    else:
        print("알림 없음", "(오류 지속: " + ", ".join(st["errors"]) + ")" if st["errors"] else "")

    if not a.dry_run or a.save:
        statemod.save(a.state, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
