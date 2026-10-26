"""Library entry point (P3): audit(dataset, requests=None, ...) -> AuditResult.

The CLI (code/main.py) and any other caller run audits through audit(); there is no second audit path.
It runs the same steps the CLI always ran, in the same order: request files and CSV inputs are validated,
pipeline.run (M1-M5) runs once over all request files, then audit_cases reads intent and assembles each
requested row with final.assemble. The structured result is copied from what those steps return:
nothing is recalculated or reinterpreted here.

Errors: invalid input or configuration raises inputs.InputError (policy.PolicyError and
settings.SettingsError are subclasses) and writes nothing. Any other exception is an internal failure
and propagates unchanged. A returned AuditResult is a completed audit; its warnings may be non-empty.
No module-level state is written: policy and settings are passed down per call, so audits in one
process don't affect each other.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from audit import final, ingest, inputs, intent, llm_intent, pipeline, policy as policy_mod, settings as settings_mod
from audit.inputs import InputError
from audit.matching import _ts
from audit.output import write_output
from audit.state import _iso


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    deletion_status: str  # COMPLETE | INCOMPLETE | NOT_APPLICABLE, as written to the CSV
    steps: list           # the row's steps_json array, exactly as written to the CSV
    trace: dict           # final.assemble's trace (basis, blockers[, system_from_m2_link]) + intent, intent_rule, intent_llm
    evaluations: list     # this request's M5 evaluations (policy.Evaluation.to_dict), in the order final.assemble reads them
    remediations: list    # the M5 remediations of this request's findings (policy.Remediation)


@dataclass(frozen=True)
class AuditResult:
    dataset: str          # paths as given, with forward slashes
    requests: str
    output: str | None    # the CSV written, or None when no out path was given
    cases: list           # [CaseResult] in request-file order (the CSV's row order)
    summary: dict         # rows, status counts, basis counts, steps (what the CLI prints)
    warnings: list        # warning texts, in the order the CLI prints them to stderr
    diagnostics: dict     # files (per evidence file), parser (ingest diagnostics), coverage (ingest.coverage)

    def to_dict(self):
        """-> a JSON-compatible dict (only dicts, lists, str, int, float, bool, None)."""
        return asdict(self)

    def case(self, case_id):
        return next(c for c in self.cases if c.case_id == case_id)


def audit_cases(requests, result, warnings=None):
    """-> [(case_id, status, steps, trace)] for the requested rows, from one pipeline result.
    Intent warnings are appended to `warnings` (the CLI prints them)."""
    warnings = [] if warnings is None else warnings
    known = {r["case_id"] for r in result["rows"]}
    missing = [r["case_id"] for r in requests if r["case_id"] not in known]
    if missing:
        raise InputError(f"requests not in the dataset's request files (needed for joint matching): {missing[:5]}")
    evaluations = defaultdict(list)
    for e in result["E"]:
        evaluations[e.case_id].append(e)
    remediation_of = {r.finding_id: r for r in result["R"]}
    fallback = final.link_systems(result["links"])
    llm = llm_intent.labels({r["request_text"] for r in requests})
    out = []
    for req in requests:  # input order is kept: case_33..case_550 ascending by case number
        rule, read = intent.classify(req["request_text"]), llm[req["request_text"]]
        label = intent.decide(rule, read)
        if label == intent.AMBIGUOUS:
            warnings.append(f"ambiguous intent audited as deletion: {req['case_id']}")
        elif read not in (None, rule):
            warnings.append(f"rule {rule} / LLM {read} -> {label}: {req['case_id']}")
        status, steps, trace = final.assemble(req["case_id"], label, evaluations[req["case_id"]], remediation_of,
                                              _iso(_ts(req["audit_at"])), fallback)
        out.append((req["case_id"], status, steps, dict(trace, intent=label, intent_rule=rule, intent_llm=read)))
    return out


def audit(dataset, requests=None, *, out=None, policy=None, settings=None):
    """Audit `dataset` and return an AuditResult.

    requests: the request file whose rows are reported (default <dataset>/deletion_requests.csv). Its rows
      must also be in one of the dataset's request files, which are always matched jointly.
    out: write the output CSV here (byte-identical to the CLI's); None writes nothing.
    policy: a policy CSV path, or a matrix from policy.load_policy (default: code/audit/deletion_rules.csv).
    settings: a settings JSON path, or a settings.Settings (default: settings.DEFAULT).
    Raises InputError (or its PolicyError / SettingsError subclasses) for invalid input; nothing is written."""
    dataset = Path(dataset)
    requests = dataset / "deletion_requests.csv" if requests is None else Path(requests)
    if policy is not None and not isinstance(policy, dict):
        policy = policy_mod.load_policy(policy)
    if settings is None:
        settings = settings_mod.DEFAULT
    elif not isinstance(settings, settings_mod.Settings):
        settings = settings_mod.load_settings(settings)
    joint = pipeline.request_files(dataset)  # checked before the pipeline runs
    if not requests.is_file():
        raise InputError(f"{requests}: request file not found (choose one with --requests)")
    own = [] if requests.resolve() in {p.resolve() for p in joint} else [requests]
    inputs.validate(dataset, joint, own)  # one report covering --requests too; pipeline.run re-checks the dataset
    result = pipeline.run(dataset, policy_matrix=policy, settings=settings)
    warnings = list(result["warnings"])
    rows = audit_cases(pipeline.read_requests(requests), result, warnings)
    by_case = defaultdict(list)
    for e in result["E"]:
        by_case[e.case_id].append(e)
    remediation_of = {r.finding_id: r for r in result["R"]}
    cases = []
    for case_id, status, steps, trace in rows:
        evaluations = sorted(by_case[case_id], key=lambda e: e.resource_id)  # as final.assemble sorts them
        cases.append(CaseResult(case_id, status, steps, trace, [e.to_dict() for e in evaluations],
                                [asdict(remediation_of[e.finding.finding_id]) for e in evaluations if e.finding]))
    obs_per_file = Counter(o.file_id for o in result["obs"])
    diags_per_file = Counter(d["file_id"] for d in result["diags"])
    coverage = ingest.coverage(dataset, result["manifest"], result["obs"])
    coverage["other_id_tokens"] = dict(coverage["other_id_tokens"])
    diagnostics = {
        "files": [{"file_id": m["file_id"], "file_path": m["file_path"], "format": m["format"],
                   "observations": obs_per_file[m["file_id"]], "diagnostics": diags_per_file[m["file_id"]]}
                  for m in result["manifest"]],
        "parser": result["diags"],
        "coverage": coverage,
    }
    summary = {"rows": len(rows), "status": dict(Counter(r[1] for r in rows)),
               "basis": dict(Counter(r[3]["basis"] for r in rows)), "steps": sum(len(r[2]) for r in rows)}
    if out is not None:
        write_output([(c, s, steps) for c, s, steps, _ in rows], out)
    return AuditResult(dataset.as_posix(), requests.as_posix(), None if out is None else Path(out).as_posix(),
                       cases, summary, warnings, diagnostics)
