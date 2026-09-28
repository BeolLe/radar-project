import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import ts from "typescript";
import { tagState } from "../lib/tag-state.ts";

assert.equal(tagState(null, null), "AI 잠정 분류 · URL 미확인");
assert.equal(tagState(null, "unknown"), "AI 잠정 분류 · URL 미확인");
assert.equal(tagState(null, "needs_review"), "응답 불일치 · 검토 대기 · URL 미확인");
assert.equal(tagState("detail", "fetch_failed"), "URL 조회 실패 · 재점검 필요");
assert.equal(tagState("preliminary", "classified"), "URL 미확인");
assert.equal(tagState("detail", "classified"), "URL 기반 AI 점검 완료");
// A failed/unknown attempt must never be presented as completed or still waiting.
assert.equal(tagState("detail", "unknown"), "분류 보류 · 근거 부족");
console.log("Tag state checks passed");

// Exercise the real component's timer/effect with tiny browser/hook fakes, no test framework.
let timer, visibilityListener, cleanup, refreshes = 0, cleared = false;
const document = { visibilityState: "visible", activeElement: null,
  addEventListener(name, fn) { assert.equal(name, "visibilitychange"); visibilityListener = fn; },
  removeEventListener(name, fn) { assert.equal(fn, visibilityListener); visibilityListener = null; },
};
const router = { refresh() { refreshes++; } };
const source = readFileSync(new URL("../app/auto-refresh.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
}}).outputText;
const require = createRequire(import.meta.url);
const mod = { exports: {} };
vm.runInNewContext(compiled, {
  exports: mod.exports, module: mod, document,
  window: {
    setInterval(fn, delay) { assert.equal(delay, 15000); timer = fn; return 1; },
    clearInterval(id) { assert.equal(id, 1); cleared = true; },
  },
  require(name) {
    if (name === "react") return {
      useCallback: fn => fn,
      useEffect: fn => { cleanup = fn(); },
      useTransition: () => [false, fn => fn()],
    };
    if (name === "next/navigation") return { useRouter: () => router };
    return require(name);
  },
});
const component = mod.exports.default();
timer();
assert.equal(refreshes, 1);
document.visibilityState = "hidden";
timer();
assert.equal(refreshes, 1);
document.visibilityState = "visible";
document.activeElement = { matches: () => true };
timer();
assert.equal(refreshes, 1);
component.props.children[1].props.onClick();
assert.equal(refreshes, 2); // Manual refresh remains available while a form field is focused.
document.activeElement = null;
visibilityListener();
assert.equal(refreshes, 3);
cleanup();
assert.equal(cleared, true);
assert.equal(visibilityListener, null);
console.log("Auto-refresh timer, visibility, input and cleanup checks passed");

// Render the real detail page: review fields remain visible as escaped audit text, not tag chips.
const detailSource = readFileSync(new URL("../app/domain/[id]/page.tsx", import.meta.url), "utf8");
const detailModule = { exports: {} };
const reviewed = { phase: "preliminary", status: "unknown", model: "fixture-model",
  checked: "2026-09-28", tags: [], review: { code: "status_tags_mismatch", original_result: {
    status: "unknown", tags: [{ code: "role.api", confidence: .8 }],
    reason: "</pre><script>example</script>",
  } } };
const queryResults = [[{ name: "example.com" }], [], [reviewed]];
vm.runInNewContext(ts.transpileModule(detailSource, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
}}).outputText, {
  exports: detailModule.exports, module: detailModule,
  require(name) {
    if (name.endsWith("/db")) return { filters: () => ({ kind: "daily", location: "KR" }),
      query: async () => queryResults.shift(), signalNames: {}, tagNames: {} };
    if (name.endsWith("/tag-state")) return { tagState };
    if (name.endsWith("/auto-refresh")) return { default: () => null };
    if (name === "next/link") return { default: ({ children }) => children };
    if (name === "next/navigation") return { notFound: () => assert.fail("unexpected notFound") };
    return require(name);
  },
});
const detail = await detailModule.exports.default({ params: Promise.resolve({ id: "1" }),
  searchParams: Promise.resolve({}) });
const html = require("react-dom/server").renderToStaticMarkup(detail);
assert.ok(html.includes("응답 불일치 · 검토 대기"));
assert.ok(html.includes("불일치 응답 원본 보기 · 확정 태그 아님"));
assert.ok(html.includes("role.api") && html.includes("0.8"));
assert.ok(html.includes("&lt;script&gt;"));
assert.ok(!html.includes("<script>") && !html.includes('class="chip'));
assert.ok(!html.includes("근거 부족으로 보류"));
console.log("Review detail provenance and HTML escaping checks passed");
