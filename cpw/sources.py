"""데이터 출처: 청패 목록, 서울시향 일정, NOL(구 인터파크) 상품·회차·잔여석.

모두 로그인 없이 응답하는 공개 엔드포인트입니다(2026-11 확인).
"""
import html
import re

from .net import ParseError, fetch, fetch_json

CP_LIST = "https://api.youthcultureseoul.kr/svc/svcYouthCultureSeoul/selectGoodsMasterList"
CP_PAGE = "https://www.youthcultureseoul.kr/youthCultureSeoul"
SP_LIST = "https://www.seoulphil.or.kr/perf/selectPerfList"
SP_VIEW = "https://www.seoulphil.or.kr/perf/view?perfNo={}"
NOL_PAGE = "https://nol.yanolja.com/ticket/products/{}"
NOL_API = "https://nol.yanolja.com/ticket/products/api"


def nol_url(code: str) -> str:
    return NOL_PAGE.format(code)


# ── 청패 ───────────────────────────────────────────────
def chungpae_list(genre: str) -> list[dict]:
    items: dict[str, dict] = {}
    page = 1
    while page <= 20:
        d = fetch_json(CP_LIST, params={
            "siteID": "YOUTHCULTURESEOUL", "deviceType": "pc", "displayGoodsCode": genre,
            "page": page, "pageSize": 100, "searchText": "", "orderByRanking": "orderByRanking",
        }, headers={"Referer": CP_PAGE})
        if str(d.get("result")) != "200" or not isinstance(d.get("items"), list):
            raise ParseError(f"청패 목록 응답 구조가 다름: result={d.get('result')}")
        for it in d["items"]:
            if "goodsCode" not in it or "goodsName" not in it:
                raise ParseError("청패 목록 항목에 goodsCode/goodsName 없음")
            items[it["goodsCode"]] = it
        if not d["items"] or page >= int(d.get("pageCount") or 1):
            break
        page += 1
    out = []
    for it in items.values():
        out.append({
            "key": f"cp:{it['goodsCode']}",
            "source": "청패",
            "goods": it["goodsCode"],
            "title": it["goodsName"].strip(),
            "venue": it.get("placeName") or "",
            "place_code": it.get("placeCode") or "",
            "start": it.get("startDate") or "",
            "end": it.get("endDate") or "",
            "biz": it.get("bizInfo") or "",
            "registered": (it.get("registerDateForWeb") or "")[:10],
            "bookable": it.get("onOffName") == "예매가능",
            "chungpae": True,
            "link": nol_url(it["goodsCode"]),
        })
    return out


# ── 서울시향 ────────────────────────────────────────────
def seoulphil_list() -> list[dict]:
    seen: dict[str, dict] = {}
    total = None
    for page in range(1, 15):
        d = fetch_json(SP_LIST, data={
            "pageIndex": page, "searchKeyword": "", "searchDate": "", "perfCate1": "", "tabType": "",
        }, headers={"X-Requested-With": "XMLHttpRequest", "ajax": "true",
                    "Referer": "https://www.seoulphil.or.kr/perf/list"})
        lst = d.get("perfList")
        if not isinstance(lst, list):
            raise ParseError("서울시향 목록 응답에 perfList 없음")
        total = int(d.get("perfTotalCnt") or 0)
        new = 0
        for p in lst:
            if "perfNo" not in p or "perfName" not in p:
                raise ParseError("서울시향 항목에 perfNo/perfName 없음")
            if p["perfNo"] not in seen:
                seen[p["perfNo"]] = p
                new += 1
        if not new or len(seen) >= total:
            break
    out = []
    for p in seen.values():
        b = p.get("beginDate") or ""  # 202611261930
        date = f"{b[:4]}-{b[4:6]}-{b[6:8]}" if len(b) >= 8 else ""
        out.append({
            "key": f"sp:{p['perfNo']}",
            "source": "서울시향",
            "goods": p.get("interparkCode") or "",
            "title": html.unescape(p["perfName"]).strip(),
            "venue": p.get("placeNm") or "",
            "place_code": "",
            "start": date,
            "end": date,
            "time": f"{b[8:10]}:{b[10:12]}" if len(b) >= 12 else "",
            "biz": "서울시립교향악단",
            "registered": (p.get("regDate") or "")[:10],
            "bookable": True,
            "chungpae": False,
            "link": SP_VIEW.format(p["perfNo"]),
        })
    return out


# ── NOL ────────────────────────────────────────────────
_PRICE = re.compile(r'"seatGrade":"(\w+)","priceGrade":"(\w+)","seatGradeName":"([^"]+)",'
                    r'"priceGradeName":"([^"]*)","salesPrice":(\d+)')


def nol_product(code: str) -> dict:
    """상품 페이지 HTML에서 공연장 코드, 이름, 등급별 정가, 티켓 오픈 시각을 읽는다."""
    raw = fetch(NOL_PAGE.format(code))
    h = raw.replace('\\"', '"')
    m = re.search(r'"goodsKey":"' + re.escape(code) + r':(\w+)"', h)
    if not m:
        raise ParseError(f"NOL 상품 {code}: goodsKey 없음")
    place = m.group(1)
    info = re.search(r'"goodsCode":"' + re.escape(code) +
                     r'","goodsName":"([^"]*)","placeCode":"\w+","placeName":"([^"]*)"', h)
    grades: dict[str, dict] = {}
    for sg, _pg, sname, pname, price in _PRICE.findall(h):
        price = int(price)
        cur = grades.get(sg)
        is_general = "일반" in pname
        if cur is None or (is_general and not cur["general"]) or \
                (is_general == cur["general"] and price > cur["price"]):
            grades[sg] = {"name": sname, "price": price, "general": is_general}
    op = re.search(r'"bookingOpenTime":"([^"]+)"', h)
    return {
        "place_code": place,
        "title": info.group(1) if info else "",
        "venue": info.group(2) if info else "",
        "grades": {k: {"name": v["name"], "price": v["price"]} for k, v in grades.items()},
        "open_time": op.group(1) if op else "",
    }


def nol_schedules(code: str, place: str, start: str, end: str) -> list[dict]:
    d = fetch_json(f"{NOL_API}/schedules", params={
        "goodsKey": f"{code}:{place}", "playStartDate": start, "playEndDate": end})
    if not isinstance(d.get("content"), list):
        raise ParseError(f"NOL 회차 응답 구조가 다름 ({code})")
    return d["content"]


def nol_remaining(code: str, seq: str) -> dict[str, int]:
    d = fetch_json(f"{NOL_API}/remaining-seats", params={"goodsCode": code, "playSeq": seq})
    rs = d.get("remainSeat")
    if not isinstance(rs, list):
        raise ParseError(f"NOL 잔여석 응답 구조가 다름 ({code}/{seq})")
    return {str(x["seatGrade"]): int(x["remainCnt"]) for x in rs}


def nol_pinned(code: str) -> int:
    d = fetch_json(f"{NOL_API}/pinned-count", params={"goodsCode": code})
    if "count" not in d:
        raise ParseError(f"NOL 찜 수 응답 구조가 다름 ({code})")
    return int(d["count"])
