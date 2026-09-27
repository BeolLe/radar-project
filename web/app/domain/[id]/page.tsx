import Link from "next/link";
import { notFound } from "next/navigation";
import { filters, query, signalNames, tagNames, type Params, type Tag } from "../../../lib/db";

export const dynamic = "force-dynamic";

export default async function Domain({ params, searchParams }: {
  params: Promise<{ id: string }>; searchParams: Promise<Params>;
}) {
  const { id } = await params;
  if (!/^\d{1,18}$/.test(id)) notFound();
  const { kind, location } = filters(await searchParams);
  const domains = await query<{ name: string }>("SELECT name FROM core.domain WHERE id=$1", [id]);
  if (!domains[0]) notFound();
  const history = await query<{ day: string; value: number; signals: string[] }>(`
    SELECT s.period_date::text AS day,o.value,
      ARRAY(SELECT signal FROM mart.domain_signal WHERE snapshot_id=s.id AND domain_id=o.domain_id) AS signals
    FROM core.observation o JOIN core.snapshot s ON s.id=o.snapshot_id
    WHERE o.domain_id=$1 AND s.kind=$2 AND s.location=$3
    ORDER BY s.period_date DESC LIMIT 366`, [id, kind, location]);
  const tags = await query<{ phase: string; status: string; model: string; checked: string; tags: Tag[] }>(`
    SELECT phase,status,model,checked_at::text AS checked,tags FROM core.tag_result
    WHERE domain_id=$1 ORDER BY imported_at DESC,id DESC LIMIT 20`, [id]);
  return <>
    <Link href={`/?kind=${kind}&location=${location}`}>← 목록</Link>
    <p className="eyebrow">DOMAIN HISTORY</p><h1>{domains[0].name}</h1>
    <a href={`https://${domains[0].name}`} target="_blank" rel="noopener noreferrer">원사이트 열기 ↗</a>
    <h2>관측 이력 · {location} / {kind === "weekly" ? "주간 구간" : "일간 순위"}</h2>
    <div className="table-wrap"><table><thead><tr><th>기준일</th><th>{kind === "weekly" ? "구간 상한" : "순위"}</th><th>변화</th></tr></thead><tbody>
      {history.map(h => <tr key={h.day}><td>{h.day}</td><td>{kind === "weekly" ? "≤ " : ""}{h.value.toLocaleString("ko-KR")}</td><td>{h.signals.map(s => signalNames[s] ?? s).join(" · ") || "—"}</td></tr>)}
    </tbody></table></div>
    <p className="muted">관측된 날짜만 표시합니다. 빈 기간을 자동으로 순위권 이탈로 해석하지 않습니다.</p>
    <h2>Gemini API 태깅 이력</h2>
    {!tags.length && <p className="empty">아직 태깅 결과가 없습니다.</p>}
    {tags.map((t, i) => <section className="tag-record" key={i}>
      <strong>{t.phase === "detail" ? "URL 상세 점검" : "잠정 분류"} · {t.status}</strong>
      <p>{t.tags.map(tag => `${tagNames[tag.code] ?? tag.code} (${tag.confidence.toFixed(2)})`).join(" · ") || "분류 없음"}</p>
      <small>{t.checked} · {t.model}</small>
    </section>)}
  </>;
}
