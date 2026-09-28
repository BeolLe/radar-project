export function tagState(phase: string | null, status: string | null): string {
  if (status === "unknown") return "분류 보류 · 근거 부족";
  if (status === "fetch_failed") return "URL 조회 실패 · 재점검 필요";
  if (status === "classified") {
    return phase === "detail" ? "URL 기반 AI 점검 완료" : "AI 잠정 분류 · URL 미확인";
  }
  return "태그 수집·분류 대기";
}
