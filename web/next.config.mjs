import path from "node:path";

/** @type {import('next').NextConfig} */
export default {
  poweredByHeader: false,
  outputFileTracingRoot: path.resolve(process.cwd(), ".."),
};
