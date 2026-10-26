// The one Node API plain.test.ts uses (reading the engine's source); the app itself never touches Node.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string; // relative to web/, where vitest runs
}
