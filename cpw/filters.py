"""공연 선별: 공연장 → 제외어 → 티어(서울시향/국내 상위권/해외·스타) → 저가석 유무."""


def venue_ok(venue: str, cfg: dict) -> bool:
    v = (venue or "").replace(" ", "")
    return any(x.replace(" ", "") in v for x in cfg["filter"]["venues"])


def excluded(perf: dict, cfg: dict) -> bool:
    text = f"{perf['title']} {perf.get('biz', '')}"
    return any(k and k in text for k in cfg["filter"].get("exclude_keywords", []))


def usable_grades(grades: dict, cfg: dict) -> dict:
    bad = cfg["filter"].get("exclude_grade_keywords", [])
    return {k: g for k, g in grades.items() if not any(b in g["name"] for b in bad)}


def cheap_grades(grades: dict, max_price: int, cfg: dict) -> dict:
    return {k: g for k, g in usable_grades(grades, cfg).items() if g["price"] <= max_price}


def match_tier(perf: dict, grades: dict | None, cfg: dict):
    """맞는 티어와 이유를 돌려준다. 없으면 (None, '')."""
    text = f"{perf['title']} {perf.get('biz', '')}"
    if perf.get("source") == "서울시향":
        return cfg["filter"]["tiers"][0], "서울시향 홈페이지 공연"
    for tier in cfg["filter"]["tiers"]:
        for kw in tier.get("keywords", []):
            if kw and kw in text:
                return tier, f"'{kw}'"
        top = tier.get("min_top_price", 0)
        if top and grades:
            mx = max((g["price"] for g in usable_grades(grades, cfg).values()), default=0)
            if mx >= top:
                return tier, f"최고가 {mx:,}원(대형 공연)"
    return None, ""
