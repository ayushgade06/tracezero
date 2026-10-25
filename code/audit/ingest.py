"""Evidence ingestion: manifest -> parsers -> observations + diagnostics, plus the
resource-id coverage audit against the raw evidence text.

Everything is regenerated from dataset/ at runtime (deterministic, manifest order).
The one stored derivative is the raw OCR box cache for the 11 screenshots (code/cache/ocr,
see audit/ocr.py), so clean checkouts do not need the optional OCR engine.
"""
import csv
from collections import Counter
from pathlib import Path

from audit import ocr, parsers
from audit.observation import ID_TOKEN_RE, RESOURCE_RE, USER_RE, parse_ts

FORMATS = {".sql": "sql", ".json": "json", ".log": "log", ".txt": "txt", ".pdf": "pdf", ".png": "image"}


def load_manifest(dataset):
    with open(Path(dataset) / "evidence_files.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        ext = Path(r["file_path"]).suffix.lower()
        out.append({
            "file_id": r["file_id"], "file_path": r["file_path"], "origin": r["origin"],
            "captured_at_raw": r["captured_at"], "captured_at": parse_ts(r["captured_at"]),
            "format": FORMATS.get(ext, "unknown"),
        })
    return out


def pdf_pages(path):
    from pypdf import PdfReader  # only needed for the 4 PDF documents
    return [page.extract_text() or "" for page in PdfReader(str(path)).pages]


def raw_text(meta, dataset):
    """The text a file's observations are derived from (PDF: extracted text)."""
    path = Path(dataset) / meta["file_path"]
    if meta["format"] == "pdf":
        return "\n".join(pdf_pages(path))
    if meta["format"] == "image":  # OCR text from the validated cache, if available
        boxes, _, _ = ocr.load_boxes(meta, dataset) if _has_ocr_cache(meta) else (None, None, None)
        return None if boxes is None else "\n".join(b[4] for b in boxes)
    if meta["format"] == "unknown":
        return None
    return path.read_text(encoding="utf-8")


def _has_ocr_cache(meta):
    return (ocr.CACHE_DIR / f"{meta['file_id']}.json").exists()


def parse_file(meta, dataset, use_ocr=True, aliases=None):
    path = Path(dataset) / meta["file_path"]
    loc = {"file_path": meta["file_path"]}
    if not path.exists():
        return [], [parsers._diag(meta, loc, "manifest entry has no file", severity="error")]
    if meta["captured_at"] is None:
        return [], [parsers._diag(meta, loc, f"unparseable manifest captured_at {meta['captured_at_raw']!r}", severity="error")]
    fmt = meta["format"]
    try:
        if fmt == "pdf":
            return parsers.parse_pdf_pages(meta, pdf_pages(path))
        if fmt == "image":
            if not use_ocr:
                return [], [parsers._diag(meta, loc, "image not parsed (OCR disabled)", severity="info")]
            return ocr.parse_image(meta, dataset, aliases if aliases is not None else ocr.load_aliases(dataset))
        parse = {"sql": parsers.parse_sql, "json": parsers.parse_json, "log": parsers.parse_log, "txt": parsers.parse_txt}.get(fmt)
        if parse is None:
            return [], [parsers._diag(meta, loc, "unsupported file format", severity="error")]
        return parse(meta, path.read_text(encoding="utf-8"))
    except Exception as e:  # report and keep going; never drop a file silently
        return [], [parsers._diag(meta, loc, f"parser error: {type(e).__name__}: {e}", severity="error")]


def ingest(dataset, use_ocr=True):
    """-> (manifest, observations, diagnostics), all in manifest/file order.
    use_ocr=False reproduces the pre-M6 behaviour (screenshots reported, not parsed)."""
    manifest = load_manifest(dataset)
    aliases = ocr.load_aliases(dataset) if use_ocr else {}
    observations, diagnostics = [], []
    for meta in manifest:
        o, d = parse_file(meta, dataset, use_ocr, aliases)
        observations += o
        diagnostics += d
    return manifest, observations, diagnostics


def coverage(dataset, manifest, observations):
    """Every resource-like id in a file's raw text must be represented by an observation of
    that file (as resource_id or related_resource_ids); otherwise it is listed as unresolved
    with its line number. Other id-like tokens (not user_*, not resource prefixes) are listed
    separately so nothing is skipped silently."""
    represented = {}
    for o in observations:
        s = represented.setdefault(o.file_id, set())
        if o.resource_id:
            s.add(o.resource_id)
        s.update(o.related_resource_ids)
    report = {"files": 0, "tokens": 0, "represented": 0, "unresolved": [], "other_id_tokens": Counter(), "other_examples": {}}
    for meta in manifest:
        text = raw_text(meta, dataset)
        if text is None:
            continue
        report["files"] += 1
        first_line = {}
        for lineno, line in enumerate(text.splitlines(), 1):
            for tok in RESOURCE_RE.findall(line):
                first_line.setdefault(tok, (lineno, line.strip()))
            for tok in ID_TOKEN_RE.findall(line):
                if not RESOURCE_RE.fullmatch(tok) and not USER_RE.fullmatch(tok):
                    prefix = tok.rsplit("_", 1)[0]
                    report["other_id_tokens"][prefix] += 1
                    report["other_examples"].setdefault(prefix, f"{meta['file_id']} L{lineno}: {tok}")
        have = represented.get(meta["file_id"], set())
        report["tokens"] += len(first_line)
        for tok, (lineno, line) in sorted(first_line.items()):
            if tok in have:
                report["represented"] += 1
            else:
                report["unresolved"].append({"file_id": meta["file_id"], "token": tok, "line": lineno, "excerpt": line[:200]})
    return report
