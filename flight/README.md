# 떨이 항공권 감시

매일 아침 9시(KST)에 GitHub Actions가 돌면서 인천/김포 출발 항공권 중 **평소보다 확 싸진 것**을 찾아 메일로 알려줍니다.

## 동작 방식

1. **넓게 훑기**: Google Travel Explore로 인천/김포 출발 "어디든" 최저가를 주말·1주 기준으로 가져와 `data/history.json`에 쌓습니다 (검색 2회)
2. **후보 고르기**: 오늘 가격이 그동안 기록의 중앙값보다 10% 이상 싼 도착지를 하루 1곳 고릅니다
3. **정밀 검사**: 그 도착지를 3·4·5·6·8일 일정으로 Google Flights에서 검색합니다 (검색 5회)
   - 경유 체류는 **6시간 이하 또는 24시간 이상**만 통과
   - 수하물 포함 표시가 있으면 0원, "Checked baggage for a fee"가 붙으면 항공사별 추정 요금을 더하고, 표시가 없으면 해외 LCC만 더해서 **수하물 포함 가격**으로 비교
   - Google의 "평소 가격대" 하한보다 25% 이상 싸면 🔥, 10% 이상이면 👀
4. **메일**: 통과한 건을 Claude(Haiku 5.5)가 짧게 요약하고 Gmail로 보냅니다. 이미 알린 일정은 3% 이상 더 내려가야 다시 알립니다

하루 약 7회 검색이라 SerpApi 무료 플랜(월 250회) 안에서 돌아갑니다. 한도에 가까워지면 스크립트가 알아서 멈춥니다(`monthly_search_limit: 240`).

> 처음 3일은 비교할 기록을 모으는 기간이라 알림이 오지 않습니다.

## 설치

### 1. 키 3개 준비

| 이름 | 받는 곳 |
|---|---|
| `SERPAPI_KEY` | serpapi.com 가입 → Dashboard → Your Private API Key |
| `ANTHROPIC_API_KEY` | platform.claude.com → API Keys |
| `GMAIL_APP_PASSWORD` | Google 계정 → 보안 → 2단계 인증 켜기 → "앱 비밀번호" 생성 (16자리) |

Gmail 앱 비밀번호는 평소 로그인 비밀번호가 아니라 이 용도로 따로 만드는 비밀번호예요. 2단계 인증이 켜져 있어야 메뉴가 보입니다.

### 2. 위치

이 폴더는 공연 감시 저장소의 `flight/`에 들어 있고, 워크플로는 `.github/workflows/flight.yml`입니다. 공연 감시(`watch.py`, `watch.yml`)와는 서로 건드리지 않습니다.

### 3. Secrets

메일 Secret은 공연 감시용으로 등록해 둔 것을 그대로 씁니다.

| Secret 이름 | 용도 |
|---|---|
| `SMTP_USER` | 보내는 Gmail 주소 (이미 있음) |
| `SMTP_PASS` | Gmail 앱 비밀번호 (이미 있음) |
| `EMAIL_TO` | 받을 주소 (이미 있음) |
| `SERPAPI_KEY` | **새로 등록** |
| `ANTHROPIC_API_KEY` | **새로 등록** |

```powershell
gh secret set SERPAPI_KEY -R jiho370/chungpae-watch
gh secret set ANTHROPIC_API_KEY -R jiho370/chungpae-watch
```

### 4. 첫 실행

저장소 → **Actions** 탭 → `flight-deal-watcher` → **Run workflow**. 초록색으로 끝나고 `flight/data/` 폴더에 커밋이 생기면 성공입니다.

커밋 단계에서 권한 오류가 나면 **Settings → Actions → General → Workflow permissions**를 "Read and write permissions"로 바꿔주세요.

## 설정 바꾸기 (`config.yaml`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `trip_lengths_days` | `[3,4,5,6,8]` | 정밀 검사할 일정 길이. 줄이면 검색 횟수가 줄어듭니다 |
| `max_candidates_per_run` | `1` | 하루에 정밀 검사할 도착지 수. 2로 올리면 하루 12회라 유료 플랜이 필요해요 |
| `exclude_date_ranges` | 없음 | 출발하면 안 되는 기간 (예: 시험 기간) |
| `deal.hot_ratio` / `good_ratio` | 0.25 / 0.10 | 🔥/👀 기준 할인율 |
| `layover` | 360 / 1440분 | 경유 체류 허용 범위 |
| `baggage.airline_fee_per_leg` | 항공사별 | 위탁수하물 추정 요금 (편도) |

실행 시각은 `.github/workflows/flight.yml`의 `cron`에서 바꿉니다 (UTC 기준이라 KST보다 9시간 느립니다).

## 내 컴퓨터에서 테스트

```powershell
pip install -r requirements.txt
cd flight
$env:SERPAPI_KEY="..."; $env:GMAIL_USER="..."; $env:GMAIL_APP_PASSWORD="..."
python monitor.py --test-email   # 메일 설정 확인
python monitor.py --dry-run      # 메일·기록 저장 없이 결과만 출력 (검색 횟수는 소모됨)
```

## 한계

- 수하물 요금은 **추정치**입니다. Google 결과에 정확한 수하물 요금이 나오지 않아서 항공사별 대략적인 금액을 더합니다
- 국내 LCC는 Google에 유료 표시가 없으면 수하물 포함으로 간주합니다. 표시가 누락된 경우 실제보다 싸게 보일 수 있어요
- 경유 조건은 **가는 편만** 확인합니다. 오는 편까지 보려면 일정마다 검색이 한 번씩 더 필요해서 뺐어요
- GitHub Actions의 예약 실행은 정시보다 몇 분에서 수십 분 늦어질 수 있습니다
