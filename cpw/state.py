"""이전 상태 저장(JSON). 중복 알림 방지용."""
import json
import os

DEFAULT = {
    "initialized": False,
    "last_lists": 0,      # 목록 확인 시각(epoch)
    "goods": {},          # NOL 상품코드 → 공연 정보 + 판정 결과
    "products": {},       # NOL 상품코드 → 가격/공연장/회차 캐시
    "presale": {},        # "상품:회차" → 판매 오픈 전
    "alerted": {},        # 풀림 알림을 보낸 좌석 → 다시 매진되면 알림
    "seats": {},          # "상품:회차:등급" → 직전 잔여 수
    "next_check": {},     # 상품코드 → 다음 잔여석 확인 시각
    "errors": {},         # 오류 키 → 처음 발생 시각
}


def load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT.items():
            data.setdefault(k, v if not isinstance(v, dict) else {})
        return data
    return json.loads(json.dumps(DEFAULT))


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)
