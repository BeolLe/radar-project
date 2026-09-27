import { query } from "../../../lib/db";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    // Require the actual dashboard relations/permissions, not only a live DB socket.
    await query(`SELECT s.id FROM core.snapshot s
      LEFT JOIN core.observation o ON false
      LEFT JOIN core.domain d ON false
      LEFT JOIN mart.domain_signal m ON false
      LEFT JOIN mart.current_tag t ON false LIMIT 0`);
    return Response.json({ ready: true }, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ ready: false }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
