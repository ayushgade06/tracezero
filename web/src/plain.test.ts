import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  blockerParts, caseLabel, caseSummary, dateText, factsText, locatorText, reasonText, sourceText, stepText, verdict,
} from "./plain";
import { result } from "./test/fixture";
import type { CaseResult, Step } from "./types";

const [caseA, caseB, caseC] = result.cases as CaseResult[];
const step = (phase: string, operation: string): Step =>
  ({ phase, operation, system_id: "s", resource_id: "r", state: "", evidence_id: "", step_no: 1 });

describe("plain language", () => {
  it("labels each request from its status and basis", () => {
    expect([caseA, caseB, caseC].map((c) => caseLabel(c).label)).toEqual(["Data still found", "Can't confirm", "Not a deletion request"]);
    expect(caseLabel({ ...caseA, deletion_status: "COMPLETE" }).label).toBe("Deleted");
    expect(verdict(caseA)).toEqual({ title: "Not deleted", detail: "This person's data is still in 1 place." });
    expect(caseSummary(caseB)).toBe("1 open question");
  });

  it("words steps as problems, fixes, to-dos and confirmations", () => {
    expect(stepText(step("FINDING", "REPORT_RESTORE_RISK"))).toBe("A backup could bring the data back");
    expect(stepText(step("REMEDIATION", "DELETE_RECORD"))).toBe("Delete the record");
    expect(stepText(step("VERIFICATION", "VERIFY_RECORD_ABSENT"))).toBe("Check the record is gone");
    expect(stepText(step("VERIFICATION", "VERIFY_RECORD_ABSENT"), true)).toBe("The record is gone");
  });

  it("explains reasons and blockers, including the unknown-state sub-reasons", () => {
    expect(reasonText("RECORD_REMAINS")).toBe("The record still exists.");
    expect(blockerParts("account_2:STATE_UNKNOWN:HISTORICAL_READINGS_ONLY"))
      .toEqual({ resource: "account_2", text: "Its current state is unknown: only older records mention it." });
  });

  it("never guesses: an unknown code is shown as it is", () => {
    expect(caseLabel({ ...caseA, deletion_status: "SOMETHING_NEW" }).label).toBe("SOMETHING_NEW");
    expect(stepText(step("FINDING", "REPORT_NEW_KIND"))).toBe("REPORT_NEW_KIND");
    expect(reasonText("NEW_REASON")).toBe("NEW_REASON");
    expect(reasonText("STATE_UNKNOWN:NEW_SUB")).toBe("Its current state is unknown.");
    expect(factsText({ existence: "PRESENT", colour: "BLUE" })).toBe("the record exists, colour: blue");
  });

  it("has a plain sentence for every operation and outcome reason the engine can produce", () => {
    // Read from the engine's source (paths relative to web/, where npm test runs) so a new code fails here.
    const finalPy = readFileSync("../code/audit/final.py", "utf8");
    const policyPy = readFileSync("../code/audit/policy.py", "utf8");
    const operations = [.../^OPERATIONS = \(([^)]*)\)/m.exec(finalPy)![1].matchAll(/"([A-Z_]+)"/g)].map((m) => m[1]);
    expect(operations.length).toBe(15);
    const phase = (op: string) => (op.startsWith("REPORT_") ? "FINDING" : op.startsWith("VERIFY_") ? "VERIFICATION" : "REMEDIATION");
    expect(operations.filter((op) => stepText(step(phase(op), op)) === op)).toEqual([]);
    const reasons = [...policyPy.matchAll(/return "(?:VIOLATION|COMPLIANT|INCONCLUSIVE)", "([A-Z_]+)"[,)]/g)].map((m) => m[1]);
    expect(reasons.length).toBeGreaterThan(10);
    expect([...new Set(reasons)].filter((r) => reasonText(r) === r)).toEqual([]);
    // The ones policy.py builds at run time.
    const built = ["RECORD_REDACTED", "RECORD_ANONYMIZED", "EXPOSURE_CONFLICTING", "EXPOSURE_STALE",
      "EXPOSURE_UNKNOWN:NO_EVIDENCE", "EXPOSURE_REDACTED_NOT_ANONYMIZED", "OWNERSHIP_CANDIDATE_ONLY"];
    expect(built.filter((r) => reasonText(r) === r)).toEqual([]);
    expect(reasonText("EXPOSURE_REDACTED_NOT_ANONYMIZED")).toBe("Its personal details are hidden, but they should be anonymised.");
  });

  it("formats sources, locations and dates", () => {
    const files = new Map([["ev_2", "evidence/databases/snap.sql"]]);
    expect(locatorText({ table: "docs", row: 1, line: 12, trace_id: "t" })).toBe("table docs, row 1, line 12");
    expect(sourceText(caseA.evaluations[1].citation, files)).toBe("snap.sql, table docs, row 1, line 12");
    expect(dateText("2026-09-06T00:00:00Z")).toBe("6 Sep 2026");
    expect(dateText("not a date")).toBe("not a date");
  });
});
