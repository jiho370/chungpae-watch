# 청패 공연 감시

서울청년문화패스(청패)와 서울시향 홈페이지에 새로 올라오는 공연, 그리고 관심 공연의 저가석 취소표를 감시하다가 지메일로 알려 주는 스크립트입니다. **예매나 결제는 하지 않고 알림만 보냅니다.**

## 무엇을 알려 주나
| 알림 | 언제 |
|---|---|
| 새 공연 | 조건에 맞는 공연이 청패나 서울시향 홈페이지에 처음 올라왔을 때 |
| 저가석 풀림 | 감시 중인 공연의 저가석이 0석에서 1석 이상이 됐을 때 (취소표, 추가 오픈) |
| 다시 매진 | 알려 드린 취소표가 다시 0석이 됐을 때 |
| 티켓 오픈 | 판매 전이던 회차가 열렸는데 저가석이 있을 때 |
| 청패 등록 | 서울시향 홈페이지에만 있던 공연이 청패 목록에도 올라왔을 때 (포인트 사용 가능) |
| 오류 / 복구 | 사이트 접속 실패나 구조 변경으로 읽기에 실패했을 때 한 번, 다시 정상이 되면 한 번 |

메일 제목은 모두 `[청패알림]`으로 시작합니다. 처음 실행하면 "감시 시작" 메일로 현재 대상 공연 목록이 한 번 옵니다.

## 조건 (config.toml에서 변경)
- 공연장: 롯데콘서트홀, 예술의전당 콘서트홀
- 2027-03-31 이전에 시작하는 공연만
- 등급(티어)
  - **서울시향**: 무조건 알림. 저가석 기준 2만 원 이하
  - **국내 상위권**(KBS교향악단, 경기필, 국립심포니, 부천필, 고잉홈, 국립합창단): 2만 원 이하 좌석이 있을 때
  - **해외·스타·영화음악**(연주자·해외 단체 이름, 영화음악 키워드, 최고가 22만 원 이상인 대형 공연): 5만 원 이하 좌석이 있을 때
  - **인기**: 위에 안 걸려도 NOL 찜 수가 150을 넘고 2만 원 이하 좌석이 있을 때
- 직접 감시할 공연은 `[watch] extra_goods`에 NOL 상품코드를 넣으면 됩니다.

이름 목록은 `keywords`에 추가하거나 빼면 됩니다. 메일에 어떤 이름이나 조건에 걸렸는지 적혀 있으니, 받아 보고 조정하세요.

## 데이터 출처 (모두 로그인 없이 공개)
- 청패 목록: `api.youthcultureseoul.kr/svc/svcYouthCultureSeoul/selectGoodsMasterList`
- 서울시향 일정: `seoulphil.or.kr/perf/selectPerfList`
- NOL 가격·회차·잔여석: `nol.yanolja.com/ticket/products/{코드}` 페이지와 `/api/schedules`, `/api/remaining-seats`, `/api/pinned-count`

청패 상품코드와 NOL 상품코드는 같습니다. 청패 포인트 예매도 NOL 좌석에서 나가므로 NOL 잔여석이 곧 청패로 살 수 있는 자리입니다. 공식 API가 아니라서 사이트가 바뀌면 오류 메일이 옵니다.

## GitHub Actions로 돌리기 (PC를 꺼도 동작)
1. Google 계정에서 2단계 인증을 켜고 **앱 비밀번호**(16자리)를 만듭니다.
2. 이 폴더를 GitHub 저장소로 올립니다.
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 다음을 추가합니다.
   - `SMTP_USER`: 보내는 지메일 주소
   - `SMTP_PASS`: 앱 비밀번호
   - `EMAIL_TO`: 받는 주소 (보내는 주소와 같으면 생략 가능)
4. **Actions** 탭에서 워크플로를 켜고 "청패 공연 감시" → **Run workflow**로 한 번 실행합니다. "감시 시작" 메일이 오면 설정이 끝난 것입니다.

이후 확인 주기 (한국 시간, config.toml `[schedule]`에서 변경):

| 항목 | 주기 |
|---|---|
| 새 공연 목록 | 평일 09:00~18:00에만, 30분마다 |
| 저가석이 넉넉한 공연 | 새 공연과 같음 (평일 업무시간, 30분) |
| 저가석 매진 공연(취소표) | 01~03시 1시간 / 03~07시 쉼 / 07~08시 30분 / 그 외 5분 |

실행 시각 자체는 워크플로의 `cron`이 정합니다. 매진 공연 간격을 지금보다 짧게 바꾸려면 cron도 같이 바꿔야 합니다.

**부하**: LLM이나 유료 API를 쓰지 않아 토큰 비용은 없습니다. 평소 실행 한 번에 NOL 잔여석 조회 20여 건(각 1KB 안팎), 목록 확인 때 10건 정도가 추가됩니다. 공개 저장소라 Actions 사용 시간도 무료입니다.

**알아 둘 점**
- GitHub 예약 실행은 몇 분씩 늦어지는 일이 흔합니다.
- 공개 저장소는 Actions가 무료입니다. 비공개 저장소는 월 2,000분 한도라 지금 cron(하루 약 208회)으로는 초과합니다.
- 이전 상태는 Actions 캐시에 저장됩니다. 캐시가 사라지면 "감시 시작" 메일이 다시 오고, 개별 알림이 쏟아지지는 않습니다.
- 실행이 실패하면(예: 메일 발송 실패) GitHub가 저장소 주인에게 실패 메일을 보냅니다.

## 외부 트리거 (Cloudflare Workers) — 실행 주기 보강
GitHub 예약 실행은 붐비면 몇 시간씩 밀리거나 빠집니다(실제로 하루 4번만 돈 적 있음). 그래서 Cloudflare Workers의 정시 실행으로 5분마다 워크플로를 직접 실행시킵니다. 수동 실행 요청은 거의 바로 시작됩니다. 워크플로의 cron은 예비용으로 남겨 둡니다(같이 돌아도 코드가 중복 확인을 건너뜀).

1. **GitHub 토큰 만들기**: GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token
   - Repository access: **Only select repositories → chungpae-watch**
   - Permissions → Repository permissions → **Actions: Read and write** (나머지는 건드리지 않음)
   - 만료일을 정하고 만든 뒤 토큰을 복사
2. **Cloudflare Worker 만들기**: [dash.cloudflare.com](https://dash.cloudflare.com) 가입 → Workers & Pages → Create → Worker → 이름 `chungpae-watch-trigger` → Deploy → Edit code에서 [trigger/worker.js](trigger/worker.js) 내용을 붙여 넣고 Deploy
3. **비밀 변수**: Worker → Settings → Variables and Secrets → Add → Type **Secret**, 이름 `GH_TOKEN`, 값에 1번 토큰
4. **정시 실행**: Worker → Settings → Trigger Events → Add → Cron Triggers에 아래 세 줄을 하나씩 추가(UTC 기준)
   - `*/5 23,0-15 * * *` (KST 08~01시, 5분)
   - `*/30 22 * * *` (KST 07~08시, 30분)
   - `0 16-17 * * *` (KST 01~03시, 1시간)

**토큰 만료**: 토큰이 만료되면 트리거가 멈춥니다. 5분 주기 시간대에 실행 간격이 30분을 넘으면 `[청패알림] 오류 실행 주기` 메일이 오니, 그때 토큰을 새로 만들어 3번만 다시 하면 됩니다.

## 내 PC에서 돌리기 (대안)
Python 3.11 이상만 있으면 되고, 추가 패키지는 없습니다.

```powershell
copy .env.example .env   # 메모장으로 열어 값 채우기
python watch.py --test-mail
python watch.py --dry-run
```

Windows 작업 스케줄러에 5분마다 등록하기(폴더 경로는 실제 위치로 바꾸세요):

```powershell
schtasks /Create /TN "청패감시" /SC MINUTE /MO 5 /TR "pythonw C:\경로\chungpae-watch\watch.py" /F
```

## 명령
| 명령 | 설명 |
|---|---|
| `python watch.py` | 한 번 실행 |
| `python watch.py --dry-run` | 메일 대신 화면에 출력, 상태 저장 안 함 |
| `python watch.py --force-lists` | 주기와 상관없이 목록도 확인 |
| `python watch.py --test-mail` | 테스트 메일 발송 |
