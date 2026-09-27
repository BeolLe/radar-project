"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return <section className="empty"><h2>자료를 불러오지 못했습니다</h2><p>DB 연결과 스키마 상태를 확인해주세요. 내부 오류나 자격증명은 화면에 표시하지 않습니다.</p><button onClick={reset}>다시 시도</button></section>;
}
