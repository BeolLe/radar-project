import Link from "next/link";
import AutoRefresh from "./auto-refresh";
import { tagState } from "../lib/tag-state";
import { filters, one, query, signalNames, tagNames, type Params, type Ranking } from "../lib/db";

export const dynamic = "force-dynamic";

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  const { kind, location } = filters(params);
  const search = one(params, "q").trim().toLowerCase().slice(0, 253);
  const tag = one(params, "tag") in tagNames ? one(params, "tag") : "";
  const page = Math.min(20000, Math.max(0, Number.parseInt(one(params, "page"), 10) || 0));
  let snapshots: { id: string; day: string; published: string }[] = [];
  let locations: { location: string }[] = [];
  let rows: Ranking[] = [];
  let problem = "";
  let selected: typeof snapshots[number] | undefined;
  try {
    snapshots = await query("SELECT id,period_date::text AS day,published_at::text AS published " +
      "FROM core.snapshot WHERE kind=$1 AND location=$2 ORDER BY period_date DESC LIMIT 366", [kind, location]);
    locations = await query("SELECT DISTINCT location FROM core.snapshot ORDER BY location");
    selected = snapshots.find(s => s.day === one(params, "date")) ?? snapshots[0];
    if (selected) {
      // ponytail: OFFSET pagination for the draft; use keyset pagination for deep browsing.
      rows = await query<Ranking>(`
        SELECT d.id,d.name,o.value,t.tags,t.phase,
          COALESCE(t.status,(SELECT CASE WHEN evidence ? 'review' THEN 'needs_review' ELSE status END
                             FROM core.tag_result WHERE domain_id=d.id
                             ORDER BY imported_at DESC,id DESC LIMIT 1)) AS tag_status,
          ARRAY(SELECT signal FROM mart.domain_signal WHERE snapshot_id=o.snapshot_id AND domain_id=d.id) AS signals
        FROM core.observation o JOIN core.domain d ON d.id=o.domain_id
        LEFT JOIN mart.current_tag t ON t.domain_id=d.id
        WHERE o.snapshot_id=$1 AND d.name LIKE $2 ESCAPE '!'
          AND ($3::text='' OR t.tags @> jsonb_build_array(jsonb_build_object('code',$3::text)))
        ORDER BY o.value,d.id LIMIT 51 OFFSET $4`,
      [selected.id, search.replace(/[!%_]/g, "!$&") + "%", tag, page * 50]);
    }
  } catch {
    problem = "DB에 연결하지 못했습니다. README의 환경변수·스키마 초기화 순서를 확인해주세요.";
  }
  const base = new URLSearchParams({ kind, location, q: search, tag, date: selected?.day ?? "" });
  const link = (n: number) => { const p = new URLSearchParams(base); p.set("page", String(n)); return `/?${p}`; };
  return <>
    <p className="eyebrow">DOMAIN INTELLIGENCE / 데이터 탐색</p>
    <h1>순위보다, 변화에 집중하세요.</h1>
    <p className="intro">도메인의 관측 이력과 변화 신호를 살펴봅니다. 상세 점검 전 AI 분류는 잠정 결과로 표시됩니다.</p>
    <aside className="tag-notice" aria-label="태그 분류 안내">
      <strong>Gemini API 태그를 순차적으로 수집·분류합니다.</strong>
      <p>최신 한국 Top 100 → 다른 국가의 최신 목록 → 나머지 도메인 순서로 처리합니다.
        태그가 없으면 ‘AI 잠정 분류 · URL 미확인’, 태그가 있으면 태그와 ‘URL 미확인’을 표시합니다.
        URL 상세 점검은 별도 단계이며, 태그가 비어 있어도 근거 부족으로 분류가 보류된 경우가 있습니다.</p>
      <small>도메인 역할은 사용자용 사이트·앱, API·백엔드, CDN 등으로 구분합니다. 이름의 api·app만으로 단정하지 않습니다. 점수는 AI 자기평가이며, 1.00도 검증된 정확도 100%를 뜻하지 않습니다.</small>
    </aside>
    <AutoRefresh />
    <form className="filters" method="get">
      <label>자료<select name="kind" defaultValue={kind}><option value="weekly">글로벌 주간 구간</option><option value="daily">일간 정확 순위</option></select></label>
      <label>국가<select name="location" defaultValue={location}><option value="WORLD">글로벌</option>{locations.filter(l => l.location !== "WORLD").map(l => <option key={l.location}>{l.location}</option>)}</select></label>
      <label>기준일<select name="date" defaultValue={selected?.day ?? ""}><option value="">최신 완료본</option>{snapshots.map(s => <option key={s.id} value={s.day}>{s.day}</option>)}</select></label>
      <label>도메인<input name="q" defaultValue={search} maxLength={253} placeholder="도메인 앞부분 검색" /></label>
      <label>태그<select name="tag" defaultValue={tag}><option value="">전체 태그</option>{Object.entries(tagNames).map(([code, label]) => <option key={code} value={code}>{label}</option>)}</select></label>
      <button type="submit">조회</button>
    </form>
    {problem ? <section className="empty" role="status"><h2>연결을 기다리고 있습니다</h2><p>{problem}</p><p>샘플 자료를 자동으로 실제 데이터처럼 보여주지 않습니다.</p></section> : <>
      <div className="section-head"><h2>{kind === "weekly" ? "글로벌 순위 구간" : `${location} 일간 순위`}</h2><span>{selected?.day ?? "자료 없음"} · {page * 50 + 1}번째부터</span></div>
      <div className="table-wrap"><table><thead><tr><th>{kind === "weekly" ? "구간 상한" : "순위"}</th><th>도메인</th><th>변화 신호</th><th>도메인 역할</th><th>Gemini API 태그 / 점검 상태</th></tr></thead>
        <tbody>{rows.slice(0, 50).map(row => <tr key={row.id}>
          <td>{kind === "weekly" ? "≤ " : ""}{row.value.toLocaleString("ko-KR")}</td>
          <td><Link className="domain" href={`/domain/${row.id}?kind=${kind}&location=${location}`}>{row.name}</Link></td>
          <td>{row.signals.length ? row.signals.map(s => <span className="chip" key={s}>{signalNames[s] ?? s}</span>) : <span className="muted">—</span>}</td>
          <td>{row.tags?.some(t => t.code.startsWith("role."))
            ? row.tags.filter(t => t.code.startsWith("role.")).map(t => <span className="chip role" key={t.code}>{tagNames[t.code] ?? t.code} · {t.confidence.toFixed(2)}</span>)
            : <span className="muted">역할 미확인</span>}</td>
          <td>{row.tags?.filter(t => !t.code.startsWith("role.")).map(t => <span className="chip neutral" key={t.code}>{tagNames[t.code] ?? t.code} · {t.confidence.toFixed(2)}</span>)}
            <small title={row.tag_status === "needs_review" ? "상태·태그가 서로 맞지 않아 원본을 보존했습니다. 도메인 상세에서 확인할 수 있습니다." : row.tag_status === "unknown" ? "분류 시도 완료 · 근거 부족으로 보류" : !row.tag_status ? "태그 수집·분류 대기" : undefined}>{tagState(row.phase, row.tag_status)}</small></td>
        </tr>)}</tbody></table></div>
      {!rows.length && <p className="empty">조건에 맞는 완료 자료가 없습니다. 처음 실행했다면 demo 명령으로 가상 데이터를 넣을 수 있습니다.</p>}
      <nav className="pagination" aria-label="페이지 이동">{page > 0 && <Link href={link(page - 1)}>← 이전</Link>}<span>{page + 1} 페이지</span>{rows.length > 50 && <Link href={link(page + 1)}>다음 →</Link>}</nav>
      {selected && <p className="muted">적재 완료 시각: {selected.published} · 주간 구간 내 개별 순위는 제공하지 않습니다.</p>}
    </>}
  </>;
}
