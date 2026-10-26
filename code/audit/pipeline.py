"""One deterministic run of M1-M5 over the whole dataset (M7).

The sample and evaluation requests are matched jointly, as in every milestone report since M3
(the one-to-one activity assignment spans both files); callers pick the rows they need.
Either request file may be absent (P2): an absent file contributes no rows, exactly like a
header-only one, and the files present are still read together in REQUEST_FILES order.
The CSV inputs are validated first (audit/inputs.py), so bad input stops with one clear report.
No evidence interpretation or policy logic lives here.
"""
import csv
from pathlib import Path

from audit import discovery, identity, ingest, inputs, matching, policy, state
from audit.inputs import InputError
from audit.settings import DEFAULT

REQUEST_FILES = ("sample_deletion_requests.csv", "deletion_requests.csv")


def request_files(dataset):
    """-> the dataset's request files that exist, in REQUEST_FILES order (at least one)."""
    if not Path(dataset).is_dir():
        raise InputError(f"{dataset}: dataset directory not found")
    found = [Path(dataset) / name for name in REQUEST_FILES if (Path(dataset) / name).is_file()]
    if not found:
        raise InputError(f"{dataset}: no request file; expected at least one of {list(REQUEST_FILES)}")
    return found


def read_requests(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(dataset, use_ocr=True, conflict_gate=True, policy_matrix=None, settings=DEFAULT):
    """-> dict with the intermediate results of every milestone (same keys as the M6 report).
    policy_matrix: a matrix from policy.load_policy (default: policy.POLICY); settings: from
    settings.load_settings (default: the reference dataset's conventions)."""
    dataset = Path(dataset)
    requests = request_files(dataset)
    inputs.validate(dataset, requests)
    manifest, obs, diags = ingest.ingest(dataset, use_ocr=use_ocr, settings=settings)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    res = discovery.classify_resources(obs, links)
    rows = [r for path in requests for r in read_requests(path)]
    events = matching.load_events(dataset)
    reqs, _, dec = matching.match(rows, events, links, index)
    A = state.reconstruct(reqs, dec, obs, links, res, events, settings)
    E, F, R = policy.run(A, policy.PolicyContext(obs, manifest, conflict_gate=conflict_gate, events=events, policy=policy_matrix))
    return {"manifest": manifest, "obs": obs, "diags": diags, "links": links, "res": res, "rows": rows,
            "dec": dec, "A": A, "E": E, "F": F, "R": R}
