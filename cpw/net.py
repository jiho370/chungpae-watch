"""HTTP 도우미: 표준 라이브러리만 사용, 요청 간 대기와 재시도 포함."""
import json
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class FetchError(Exception):
    """접속 실패, HTTP 오류."""


class ParseError(Exception):
    """응답은 왔지만 예상한 구조가 아님(사이트 구조 변경 의심)."""


_delay = 0.7
_last = 0.0


def set_delay(sec: float) -> None:
    global _delay
    _delay = max(0.0, float(sec))


def fetch(url, params=None, data=None, headers=None, timeout=20, retries=2) -> str:
    global _last
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    err = None
    for attempt in range(retries + 1):
        wait = _delay - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (attempt + 1))
    raise FetchError(f"{url.split('?')[0]} → {err}")


def fetch_json(url, **kw):
    text = fetch(url, **kw)
    try:
        return json.loads(text)
    except ValueError as e:
        raise ParseError(f"{url.split('?')[0]}: JSON 아님 ({text[:80]!r})") from e
