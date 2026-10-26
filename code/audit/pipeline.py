"""One deterministic run of M1-M5 over the whole dataset (M7).

The sample and evaluation requests are matched jointly, as in every milestone report since M3
(the one-to-one activity assignment spans both files); callers pick the rows they need.
No evidence interpretation or policy logic lives here.
"""
import csv
from pathlib import Path

from audit import discovery, identity, ingest, matching, policy, state

REQUEST_FILES = ("sample_deletion_requests.csv", "deletion_requests.csv")


def read_requests(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(dataset, use_ocr=True, conflict_gate=True, policy_matrix=None):
    """-> dict with the intermediate results of every milestone (same keys as the M6 report).
    policy_matrix: a matrix from policy.load_policy (default: policy.POLICY)."""
    dataset = Path(dataset)
    manifest, obs, diags = ingest.ingest(dataset, use_ocr=use_ocr)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    res = discovery.classify_resources(obs, links)
    rows = [r for name in REQUEST_FILES for r in read_requests(dataset / name)]
    events = matching.load_events(dataset)
    reqs, _, dec = matching.match(rows, events, links, index)
    A = state.reconstruct(reqs, dec, obs, links, res, events)
    E, F, R = policy.run(A, policy.PolicyContext(obs, manifest, conflict_gate=conflict_gate, events=events, policy=policy_matrix))
    return {"manifest": manifest, "obs": obs, "diags": diags, "links": links, "res": res, "rows": rows,
            "dec": dec, "A": A, "E": E, "F": F, "R": R}
