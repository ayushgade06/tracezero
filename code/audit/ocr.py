"""M6: OCR of the PNG screenshots into provenance-preserving M1 observations.

Four layers are kept apart:
  1. extracted text   raw OCR boxes (text, confidence, bounding box) from rapidocr, cached in
                      code/cache/ocr/<file_id>.json and keyed by image SHA-256 + engine + models +
                      configuration. Clean checkouts read the cache; the engine is optional.
  2. parsed structure geometric units on the shared admin-console template: header, KPI cards,
                      main-table rows (cells mapped to column headers), right-hand detail panel
                      (label/value pairs), activity items, top search box.
  3. observations     one Observation per unit. kind="record" only for a unit with exactly one
                      resource id in its identifying position; everything else is kind="context"
                      (kept for provenance and coverage, never a link or a state).
  4. facts            decided downstream: only VERIFIED record units can become M4 readings, and
                      only existence=PRESENT and exposure=IDENTIFIABLE (a visible email/phone).
                      A missing identifier never means redaction or absence; connector and
                      restore states are never read from screenshots.

VERIFIED = recognised unit type + the resource id token fully matches its pattern + its OCR
confidence >= MIN_SCORE. Identity keys (emails, user_ids) come only from tokens in the same unit
with confidence >= MIN_SCORE, matched exactly downstream by M2. Phone numbers are kept as text
only: OCR drops their spaces, so they cannot match the identity tables exactly. Page context
(search box, header of a list page, activity items) is never attached to table rows.
Times: the screenshot's state time is the manifest captured_at; on-screen times are context.
A backward-looking on-screen time (Created, Updated, ...) later than captured_at is reported
(OCR_ONSCREEN_TIME_AFTER_CAPTURE) and marks every unit of that file `_ocr_capture_disputed`;
the capture time is not moved and no unit is dropped (M6.1). Forward-looking labels (Expires,
Expected, Retention end) are not checked.
Catalog aliases (e.g. "object_19 (delivery_confirmation_19.pdf)") are attached only as
`_alias_candidates` on context units: a candidate mapping, never ownership or state.
"""
import csv
import hashlib
import json
import re
import struct
from pathlib import Path

from audit.observation import EMAIL_RE, RESOURCE_RE, USER_RE, Observation
from audit.settings import DEFAULT

ENGINE, ENGINE_VERSION = "rapidocr_onnxruntime", "1.4.4"
MODELS = {  # bundled with the 1.4.4 wheel; sha256 recorded from the installed package
    "det": ("ch_PP-OCRv4_det_infer.onnx", "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"),
    "rec": ("ch_PP-OCRv4_rec_infer.onnx", "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b"),
    "cls": ("ch_ppocr_mobile_v2.0_cls_infer.onnx", "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"),
}
CONFIG = {"intra_op_num_threads": 1, "inter_op_num_threads": 1}  # single thread: reproducible
PREPROCESS = "none"
MIN_SCORE = 0.90
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache" / "ocr"

# Shared admin-console template (all 11 screens; 1500 and 1800 px wide). [DATA, inspected]
SIDEBAR_X, TOPBAR_Y, HEADER_Y, KPI_Y, CONTENT_Y = 230, 60, (60, 165), (215, 305), 310
BUTTONS = {"Filter", "Export", "Actions"}
PHONE_TEXT = re.compile(r"\+\d[\d ()-]{6,}\d")
# An id-like token not preceded by a letter or "_" (so "customer_analytics_x" is not one);
# group 1 captures digits fused in front of it (a table row index).
# M6.1: labels whose value is a time that must not postdate the capture (forward-looking labels excluded).
BACKWARD_TIME_LABELS = ("created", "updated", "indexed_at", "last refreshed", "last contact", "last drill", "executed")
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_DAY_TIME = re.compile(r"(\d{1,2}) ?([A-Z][a-z]{2}) ?(\d{4})? ?(\d{2}):(\d{2}) ?UTC")
_ISO_Z = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z")
_ID_LIKE = re.compile(r"(?<![A-Za-z_])(\d*)((?:%s)_[0-9A-Za-z]+)" % "|".join(
    ("audit_record", "data_flow", "legal_hold", "account", "search", "analytics", "support",
     "export", "backup", "tombstone", "object", "marketing")))


def cache_identity(sha256):
    return {"engine": ENGINE, "engine_version": ENGINE_VERSION, "models": {k: list(v) for k, v in MODELS.items()},
            "config": CONFIG, "preprocess": PREPROCESS, "image_sha256": sha256}


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_engine(path):
    """Live OCR (optional dependency). Verifies engine version and bundled model hashes."""
    import importlib.metadata
    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR
    version = importlib.metadata.version("rapidocr-onnxruntime")
    if version != ENGINE_VERSION:
        raise RuntimeError(f"rapidocr-onnxruntime {version} installed, {ENGINE_VERSION} required")
    models = Path(rapidocr_onnxruntime.__file__).parent / "models"
    for name, digest in MODELS.values():
        if _sha256(models / name) != digest:
            raise RuntimeError(f"model {name} differs from the pinned model")
    result, _ = RapidOCR(**CONFIG)(str(path))
    boxes = []
    for quad, text, score in result or []:
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        boxes.append([round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1), text, round(float(score), 4)])
    return boxes


def load_boxes(meta, dataset, refresh=False, cache_dir=CACHE_DIR):
    """-> (boxes or None, source, diagnostics). Never writes unless refresh=True; never touches the image."""
    path = Path(dataset) / meta["file_path"]
    sha = _sha256(path)
    want = cache_identity(sha)
    cfile = Path(cache_dir) / f"{meta['file_id']}.json"
    diags = []
    if cfile.exists() and not refresh:
        cached = json.loads(cfile.read_text(encoding="utf-8"))
        if {k: cached.get(k) for k in want} == want:
            return cached["boxes"], "cache", diags
        diags.append(("error", "OCR_CACHE_MISMATCH", f"cache key differs from image/engine/config ({cfile.name})"))
    try:
        boxes = run_engine(path)
    except ImportError:
        diags.append(("error", "OCR_UNAVAILABLE", "no valid cache entry and rapidocr-onnxruntime is not installed"))
        return None, None, diags
    except Exception as e:  # report, never silently skip
        diags.append(("error", "OCR_ENGINE_ERROR", f"{type(e).__name__}: {e}"))
        return None, None, diags
    if refresh:
        cfile.parent.mkdir(parents=True, exist_ok=True)
        cfile.write_text(json.dumps(dict(want, file_id=meta["file_id"], file_path=meta["file_path"], boxes=boxes),
                                    ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8", newline="\n")
    return boxes, "live", diags


PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def png_width(path):
    """Width from the PNG IHDR chunk (stdlib only; the image file is only read)."""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG file")
    return struct.unpack(">I", head[16:20])[0]


def load_aliases(dataset):
    """Explicit catalog aliases: 'linked RID (ALIAS)' where ALIAS differs from RID."""
    aliases = {}
    with open(Path(dataset) / "system_activity.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["event_type"] == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"\b(\S+) \(([^)]+)\)", r["details"])
                if m and m.group(1) == r["resource_id"] and m.group(2) != r["resource_id"]:
                    aliases.setdefault(m.group(2), []).append({"resource_id": r["resource_id"], "activity_id": r["activity_id"]})
    return aliases


# ------------------------------------------------------------------ layout

def _boxes(raw):
    out = []
    for x0, y0, x1, y1, text, score in raw:
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "cy": (y0 + y1) / 2, "text": text.strip(), "score": score})
    return sorted(out, key=lambda b: (round(b["cy"]), b["x0"], b["text"]))


def _bands(boxes, tol=10):
    bands = []
    for b in sorted(boxes, key=lambda b: (b["cy"], b["x0"], b["text"])):
        if bands and abs(b["cy"] - bands[-1][0]["cy"]) <= tol:
            bands[-1].append(b)
        else:
            bands.append([b])
    return [sorted(band, key=lambda b: b["x0"]) for band in bands]


def layout(raw, width, marker=DEFAULT.screenshot_template_marker):
    """-> list of units {region, row, boxes, title, pairs/cells} on the shared template, which a screen
    matches when a sidebar box contains `marker` (settings.screenshot_template_marker; None: never)."""
    boxes = [b for b in _boxes(raw) if b["x0"] >= SIDEBAR_X]
    if marker is None or not any(b["x0"] < SIDEBAR_X and marker in b["text"] for b in _boxes(raw)):
        return [{"region": "unrecognised", "row": 0, "boxes": boxes}]
    units = []
    top = [b for b in boxes if b["cy"] < TOPBAR_Y and 0.2 * width < b["x0"] < 0.7 * width]
    if top:
        units.append({"region": "search_query", "row": 0, "boxes": top})
    header = [b for b in boxes if HEADER_Y[0] <= b["cy"] < HEADER_Y[1] and b["text"] not in BUTTONS]
    if header:
        units.append({"region": "header", "row": 0, "boxes": header})
    kpi = [b for b in boxes if KPI_Y[0] <= b["cy"] < KPI_Y[1]]
    labels = [b for b in kpi if b["cy"] < 245]
    pairs = []
    for v in (b for b in kpi if b["cy"] >= 245):
        lab = min(labels, key=lambda l: abs(l["x0"] - v["x0"]), default=None)
        if lab is not None and abs(lab["x0"] - v["x0"]) <= 30:
            pairs.append((lab, v))
    if pairs:
        units.append({"region": "kpi", "row": 0, "boxes": [x for p in pairs for x in p], "pairs": pairs})
    content = [b for b in boxes if b["cy"] >= CONTENT_Y]
    titles = [b for b in content if b["cy"] < CONTENT_Y + 50 and b["x0"] > 0.6 * width]
    side_x = max((b["x0"] for b in titles), default=width) - 10
    main = [b for b in content if b["x0"] < side_x]
    side = [b for b in content if b["x0"] >= side_x]
    mb = _bands(main)
    if len(mb) >= 2:
        heads = [b for b in mb[1] if not re.fullmatch(r"\d", b["text"])]
        for i, band in enumerate(mb[2:], 1):
            cells = [b for b in band if not (re.fullmatch(r"\d", b["text"]) and b["x1"] - b["x0"] < 20)]
            if not cells:
                continue
            mapped = []
            for c in cells:
                h = min(heads, key=lambda h: abs(h["x0"] - c["x0"]), default=None)
                name = h["text"] if h is not None and abs(h["x0"] - c["x0"]) <= 60 else f"col@{int(c['x0'])}"
                mapped.append((name, c))
            units.append({"region": "table_row", "row": i, "boxes": cells, "cells": mapped, "table_title": mb[0][0]["text"]})
    sb = _bands(side, tol=8)
    if sb:
        title, pairs, items, in_activity = sb[0], [], [], False
        for band in sb[1:]:
            if len(band) == 1 and band[0]["text"] == "Activity" and band[0]["x0"] < side_x + 40:
                in_activity = True
                continue
            if in_activity:
                items.append(band)
                continue
            lab = [b for b in band if b["x0"] < side_x + 60]
            val = [b for b in band if b["x0"] >= side_x + 60]
            if lab and val:
                pairs.append((lab[0], val))
            elif val and pairs:  # wrapped value line
                pairs[-1] = (pairs[-1][0], pairs[-1][1] + val)
            elif lab:  # label and value read as one box, e.g. "customer_reference user_13"
                pairs.append((lab[0], []))
        units.append({"region": "detail_panel", "row": 0, "boxes": title + [x for l, v in pairs for x in [l] + v],
                      "title": title, "pairs": pairs})
        for i, band in enumerate(items, 1):
            units.append({"region": "activity", "row": i, "boxes": band})
    return units


# ------------------------------------------------------------------ observations

def _tokens(boxes):
    """Identifier tokens per box: resource ids, emails, user ids, phone-like text, plus diagnostics."""
    out = {"resource": [], "email": [], "user_id": [], "phone_text": [], "partial": [], "ambiguous": []}
    for b in boxes:
        t = b["text"]
        for r in RESOURCE_RE.findall(t):
            out["resource"].append((r, b))
        for m in _ID_LIKE.finditer(t):
            lead, tok = m.group(1), m.group(2)
            if lead and RESOURCE_RE.fullmatch(tok):
                out["partial"].append((tok, b))       # row index fused: "3export_1259"
            elif not RESOURCE_RE.fullmatch(tok) and re.search(r"\d", tok.rsplit("_", 1)[-1]):
                out["ambiguous"].append((tok, b))     # digits misread: "audit_record_oo3"
        for e in EMAIL_RE.findall(t):
            out["email"].append((e.lower(), b))
        for u in USER_RE.findall(t):
            out["user_id"].append((u, b))
        for p in PHONE_TEXT.findall(t):
            out["phone_text"].append((p, b))
    return out


def _unit_resource(unit, toks):
    """The resource a unit is about: header/panel title or the single resource cell of a row."""
    if unit["region"] in ("header", "table_row"):
        found = sorted({r for r, _ in toks["resource"]})
        if unit["region"] == "header":
            title_and_sub = [b for b in unit["boxes"] if b["cy"] >= 95]  # breadcrumb excluded
            found = sorted({r for b in title_and_sub for r in RESOURCE_RE.findall(b["text"])})
        return found
    if unit["region"] == "detail_panel":
        return sorted({r for b in unit["title"] for r in RESOURCE_RE.findall(b["text"])})
    return []


def image_observations(meta, raw, aliases, width, source, marker=DEFAULT.screenshot_template_marker):
    """Units -> Observations + diagnostics for one screenshot (deterministic order)."""
    obs, diags = [], []
    units = layout(raw, width, marker)
    if units and units[0]["region"] == "unrecognised":
        diags.append(("warning", "UNRECOGNISED_LAYOUT", "screen does not match the admin-console template"))
    header_rid = None
    for unit in units:
        if unit["region"] == "header":
            ids = _unit_resource(unit, _tokens(unit["boxes"]))
            header_rid = ids[0] if len(ids) == 1 else None
    for unit in units:
        boxes = list(unit["boxes"])
        if unit["region"] == "kpi" and header_rid:
            continue  # record-view page: KPI cards describe the header entity, merged below
        if unit["region"] == "header" and header_rid:
            boxes += next((u["boxes"] for u in units if u["region"] == "kpi"), [])
        toks = _tokens(boxes)
        ids = _unit_resource(unit, toks) if unit["region"] in ("header", "table_row", "detail_panel") else []
        loc_id = f"{unit['region']}" + (f".row{unit['row']}" if unit["region"] in ("table_row", "activity") else "")
        bbox = [min(b["x0"] for b in boxes), min(b["y0"] for b in boxes), max(b["x1"] for b in boxes), max(b["y1"] for b in boxes)] if boxes else None
        attrs = {}
        if unit["region"] == "table_row":
            for name, c in unit["cells"]:
                attrs.setdefault(name, c["text"])
        elif unit["region"] == "detail_panel":
            attrs["title"] = " ".join(b["text"] for b in unit["title"])
            for lab, vals in unit["pairs"]:
                attrs.setdefault(lab["text"], " ".join(v["text"] for v in vals))
        else:
            for b in boxes:
                attrs.setdefault(f"text@{int(b['x0'])},{int(b['y0'])}", b["text"])
            if unit["region"] == "header" and header_rid:
                kpi = next((u for u in units if u["region"] == "kpi"), None)
                for lab, v in (kpi["pairs"] if kpi else []):
                    attrs.setdefault(f"kpi.{lab['text']}", v["text"])
        rid = ids[0] if len(ids) == 1 else None
        rid_box = next((b for r, b in toks["resource"] if r == rid), None) if rid else None
        verified = bool(rid and rid_box and rid_box["score"] >= MIN_SCORE)
        reasons = []
        if len(ids) > 1:
            reasons.append("MULTIPLE_RESOURCE_IDS")
        if rid and not verified:
            reasons.append("LOW_CONFIDENCE_RESOURCE_ID")
        for tok, b in toks["partial"]:
            diags.append(("warning", "PARTIAL_IDENTIFIER", f"{loc_id}: '{b['text']}' contains {tok} fused with other text (not extracted)"))
        for tok, b in toks["ambiguous"]:
            diags.append(("warning", "AMBIGUOUS_IDENTIFIER", f"{loc_id}: '{tok}' looks like an id but does not match its pattern (not extracted)"))
        keys, direct = [], []
        for kind in ("email", "user_id"):
            for val, b in toks[kind]:
                if b["score"] < MIN_SCORE:
                    diags.append(("info", "LOW_CONFIDENCE_TOKEN", f"{loc_id}: {kind} '{val}' score {b['score']} < {MIN_SCORE} (not used)"))
                    continue
                if unit["region"] in ("search_query", "activity"):
                    continue  # page context never carries identity to a record
                key = {"kind": kind, "value": val, "field": f"ocr:{loc_id}"}
                if key not in keys:
                    keys.append(key)
                if kind == "email" and val not in direct:
                    direct.append(val)
        for p, b in toks["phone_text"]:
            if b["score"] >= MIN_SCORE and p not in direct:
                direct.append(p)
        texts = " ".join(b["text"] for b in boxes)
        alias = sorted({a["resource_id"] for name, al in aliases.items() for a in al
                        if re.search(r"(?<![\w.])" + re.escape(name) + r"(?![\w])", texts)})
        attrs["_ocr"] = {"engine": ENGINE, "engine_version": ENGINE_VERSION, "source": source, "preprocess": PREPROCESS,
                         "min_score": min((b["score"] for b in boxes), default=None),
                         "tokens": [{"text": b["text"], "score": b["score"], "bbox": [b["x0"], b["y0"], b["x1"], b["y1"]]} for b in boxes]}
        is_record = verified and unit["region"] in ("header", "table_row", "detail_panel")
        attrs["_ocr_trust"] = "VERIFIED" if is_record else "TEXT_ONLY"
        attrs["_ocr_reasons"] = reasons
        attrs["_ocr_direct_identifiers"] = direct if is_record else []
        if alias:
            attrs["_alias_candidates"] = alias  # candidate mapping only (catalog parenthetical)
        mentioned = sorted({r for r, _ in toks["resource"]} - ({rid} if is_record else set()))
        o = Observation(
            obs_id=f"{meta['file_id']}:{loc_id}", file_id=meta["file_id"], file_path=meta["file_path"], file_format="image",
            file_origin=meta["origin"], file_captured_at=meta["captured_at"], kind="record" if is_record else "context",
            locator={"region": unit["region"], "row": unit["row"], "bbox": bbox},
            context={"unit": unit["region"], "table_title": unit.get("table_title")} if unit["region"] == "table_row" else {"unit": unit["region"]},
            resource_id=rid if is_record else None, attrs=attrs, raw=texts,
            identity_keys=keys if is_record else [], related_resource_ids=mentioned + [a for a in alias if a not in mentioned])
        obs.append(o)
    disputed = _capture_disputes(obs, meta["captured_at"])
    for d in disputed:
        diags.append(("warning", "OCR_ONSCREEN_TIME_AFTER_CAPTURE",
                      f"{d['ref']}: {d['label']} '{d['value']}' is later than the manifest capture {meta['captured_at']} "
                      "(capture time kept; units flagged, not dropped)"))
    for o in obs if disputed else ():
        o.attrs["_ocr_capture_disputed"] = disputed
    return obs, diags


def _onscreen_time(value, year):
    """'20 Jun 14:18 UTC' / '18 Jun 2026 06:42 UTC' / ISO-Z -> ISO-Z string, else None (other zones, no date)."""
    m = _DAY_TIME.fullmatch(value.strip())
    if m and m.group(2) in _MONTHS:
        return f"{m.group(3) or year}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}T{m.group(4)}:{m.group(5)}:00Z"
    m = _ISO_Z.fullmatch(value.strip())
    return m.group(0) if m else None


def _capture_disputes(obs, captured_at):
    """Backward-looking on-screen times later than the manifest capture (row and panel fields)."""
    out = []
    for o in obs:
        for label, value in sorted(o.attrs.items()):
            if label.startswith("_") or not isinstance(value, str) or label.strip().lower() not in BACKWARD_TIME_LABELS:
                continue
            t = _onscreen_time(value, captured_at[:4])
            if t and t > captured_at:
                out.append({"ref": o.obs_id, "label": label, "value": value})
    return out


def parse_image(meta, dataset, aliases, refresh=False, cache_dir=CACHE_DIR, marker=DEFAULT.screenshot_template_marker):
    """Ingest entry point for one PNG: (observations, diagnostics as M1 dicts)."""
    loc = {"file_path": meta["file_path"]}
    boxes, source, d = load_boxes(meta, dataset, refresh, cache_dir)
    diags = [{"file_id": meta["file_id"], "locator": loc, "severity": s, "reason": f"{code}: {msg}", "excerpt": ""} for s, code, msg in d]
    if boxes is None:
        return [], diags
    width = png_width(Path(dataset) / meta["file_path"])
    obs, d2 = image_observations(meta, boxes, aliases, width, source, marker)
    diags += [{"file_id": meta["file_id"], "locator": loc, "severity": s, "reason": f"{code}: {msg}", "excerpt": ""} for s, code, msg in d2]
    return obs, diags
