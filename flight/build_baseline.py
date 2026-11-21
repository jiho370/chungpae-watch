"""baseline_raw.txt(브라우저에서 모은 Google Flights 달력 통계) → data/baseline.json

raw 형식: CODE|YYMM:중앙값,하위25%,최저,일수 ...  (금액 단위 천 원)
"""
import csv
import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent

# Explore가 돌려주는 공항 코드 → baseline 도시 코드
ALIASES = {
    "NRT": "TYO", "HND": "TYO",
    "KIX": "OSA", "ITM": "OSA", "UKB": "OSA",
    "TSA": "TPE",
    "PKX": "BJS", "PEK": "BJS",
    "PVG": "SHA",
    "KTI": "PNH", "SAI": "REP", "UBN": "ULN", "TAG": "BOH",
    "CDG": "PAR", "ORY": "PAR",
    "LHR": "LON", "LGW": "LON", "STN": "LON",
    "FCO": "ROM", "CIA": "ROM",
    "JFK": "NYC", "EWR": "NYC", "LGA": "NYC",
    "YYZ": "YTO",
    "DMK": "BKK", "HLP": "CGK",
}


def main() -> None:
    meta = {}
    with open(HERE / "destinations.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            meta[row["code"]] = row

    cities = {}
    for line in (HERE / "baseline_raw.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        code, rest = line.split("|", 1)
        months = {}
        for chunk in rest.split():
            ym, vals = chunk.split(":")
            med, p25, low, n = (int(v) for v in vals.split(","))
            months[f"20{ym[:2]}-{ym[2:]}"] = {
                "median": med * 1000, "p25": p25 * 1000, "min": low * 1000, "days": n,
            }
        # 표본이 적은 달(10일 미만)은 도시 전체 중앙값 계산에서 뺀다
        solid = [m["median"] for m in months.values() if m["days"] >= 10] or [m["median"] for m in months.values()]
        m = meta.get(code, {})
        cities[code] = {
            "name": m.get("name", code),
            "region": m.get("region", ""),
            "trip_days": int(m.get("trip_days", 0) or 0),
            "median": int(statistics.median(solid)),
            "months": months,
        }

    out = {
        "source": "Google Flights 달력(서울 출발 왕복, 일반석, 수하물 미포함 최저가)",
        "collected": "2026-11-21",
        "note": "출발일마다 달력에 뜬 최저가를 모아 월별 중앙값/하위25%/최저를 냄. 여행 길이는 도시별 trip_days.",
        "aliases": ALIASES,
        "cities": cities,
    }
    path = HERE / "data" / "baseline.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(cities)}개 도시 → {path}")


if __name__ == "__main__":
    main()
