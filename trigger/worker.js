// 청패 공연 감시 외부 트리거 (Cloudflare Workers, 무료)
//
// GitHub 예약 실행(cron)은 붐비면 몇 시간씩 밀리거나 빠지기 때문에,
// Cloudflare의 정시 실행(Cron Triggers)으로 GitHub 워크플로를 직접 실행시킨다.
// 수동 실행(workflow_dispatch) 요청은 거의 지연 없이 바로 시작된다.
//
// 필요한 값: 비밀 변수 GH_TOKEN
//   = jiho370/chungpae-watch 저장소만, Actions 권한 "Read and write"만 준 fine-grained 토큰
//
// Cron Triggers (UTC 기준, KST = UTC+9) — .github/workflows/watch.yml과 같은 시간표:
//   */5 23,0-15 * * *   KST 08:00~00:59  5분마다
//   */30 22 * * *       KST 07:00~07:59  30분마다
//   0 16-17 * * *       KST 01:00~02:59  1시간마다
//   (KST 03:00~06:59 는 실행 안 함)

const REPO = "jiho370/chungpae-watch";
const WORKFLOW = "watch.yml";

async function dispatch(env) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "chungpae-watch-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (res.status !== 204) {
    // Workers 로그(대시보드 → Logs)에 남김. 토큰 만료 등은 watch.py의 '실행 간격' 오류 메일로도 알 수 있음.
    console.log(`dispatch 실패: HTTP ${res.status} ${await res.text()}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  // 주소로 접속해도 실행되지 않음(아무나 실행시키지 못하게). 상태 확인용 응답만.
  async fetch() {
    return new Response("chungpae-watch trigger: OK (정시 실행 전용)");
  },
};
