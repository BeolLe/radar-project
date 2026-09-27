import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = { title: "Radar Project", description: "도메인 순위 변화와 AI 분류 이력" };

export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ko"><body>
    <header><Link href="/" className="brand">RADAR <span>PROJECT</span></Link><span className="badge">개발 초안</span></header>
    <main>{children}</main>
    <footer>Cloudflare Radar 기반 · DNS 인기도는 방문자 수가 아닙니다. AI 태그는 모델의 판단입니다.</footer>
  </body></html>;
}
