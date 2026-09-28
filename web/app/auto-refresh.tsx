"use client";

import { useCallback, useEffect, useTransition } from "react";
import { useRouter } from "next/navigation";

export default function AutoRefresh() {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const refresh = useCallback(() => {
    if (!pending && document.visibilityState === "visible") {
      startTransition(() => router.refresh());
    }
  }, [pending, router]);

  useEffect(() => {
    const automatic = () => {
      if (!document.activeElement?.matches("input,select,textarea")) refresh();
    };
    const timer = window.setInterval(automatic, 15_000);
    document.addEventListener("visibilitychange", automatic);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", automatic);
    };
  }, [refresh]);

  return <div className="refresh-bar">
    <span className="muted">이 화면은 15초마다 최신 결과를 확인합니다. 다른 탭을 보거나 검색 조건을 입력할 때는 잠시 쉽니다.</span>
    <button type="button" onClick={refresh} disabled={pending} aria-busy={pending}>
      {pending ? "확인 중…" : "지금 업데이트"}
    </button>
  </div>;
}
