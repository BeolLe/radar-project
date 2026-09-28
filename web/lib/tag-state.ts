export function tagState(phase: string | null, status: string | null): string {
  if (status === "needs_review") return "응답 불일치 · 검토 대기 · URL 미확인";
  if (phase === "detail" && status === "unknown") return "분류 보류 · 근거 부족";
  if (status === "fetch_failed") return "URL 조회 실패 · 재점검 필요";
  if (status === "classified") {
    return phase === "detail" ? "URL 기반 AI 점검 완료" : "URL 미확인";
  }
  return "AI 잠정 분류 · URL 미확인";
}
