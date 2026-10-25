"""LLM reading of request intent (Claude), with a committed content-keyed cache.

The rule classifier (audit/intent.py) and Claude each read every request_text; audit/intent.decide
combines them. Claude is called only by `python code/main.py --refresh-intent-cache` (needs
ANTHROPIC_API_KEY and `pip install -r code/requirements-llm.txt`). Normal runs read
code/cache/llm/intent.json and make no network calls, so the output stays deterministic; a text
missing from the cache falls back to the rule label.

Unique texts are sent in batches (one call per BATCH texts) with a JSON-schema output, and every
call's token usage is kept in the cache so evaluation/usage_report.md is computed, not estimated.
"""
import hashlib
import json
from pathlib import Path

MODEL = "claude-haiku-4-5"  # cheapest current model; a short classification needs no more
PROMPT_VERSION = "intent-v1"
PRICE_PER_MTOK = {"claude-haiku-4-5": (1.00, 5.00)}  # USD per million (input, output) tokens
BATCH = 40
CACHE = Path(__file__).resolve().parents[1] / "cache" / "llm" / "intent.json"

SYSTEM = """You classify data-subject requests sent to a commerce platform's privacy team.
For each request decide whether the person authorizes deletion of their account or personal data.

DELETION: they ask to delete, erase, close or remove their account or personal data, or to finish or
confirm a deletion that already started (including asking to remove leftover copies, or to keep
only a minimal reference that policy requires after deletion).
NON_DELETION: anything else - access or copy of data, export or portability, correcting details,
login help, marketing opt-out or unsubscribe only, or any request that says to keep the account or
not delete it.

Requests may be in any language. Judge the request as a whole; a negated or limited mention of
deletion ("do not delete", "remove me from marketing lists only") is NON_DELETION.
Return one item per input id, with a reason of at most 12 English words."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"},
                       "label": {"type": "string", "enum": ["DELETION", "NON_DELETION"]},
                       "reason": {"type": "string"}},
        "required": ["id", "label", "reason"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def key(text):
    return hashlib.sha256(f"{MODEL}\n{PROMPT_VERSION}\n{text}".encode("utf-8")).hexdigest()


def load(path=CACHE):
    if not Path(path).exists():
        return {"model": MODEL, "prompt_version": PROMPT_VERSION, "entries": {}, "calls": []}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def labels(texts, path=CACHE):
    """-> {text: "DELETION" | "NON_DELETION" | None}; cache only, never calls the API."""
    entries = load(path)["entries"]
    return {t: (entries.get(key(t)) or {}).get("label") for t in texts}


def refresh(texts, path=CACHE, client=None):
    """Classify every text missing from the cache with Claude, in batches; -> number of calls made."""
    cache = load(path)
    todo = sorted({t for t in texts if key(t) not in cache["entries"]})
    if todo and client is None:
        import anthropic  # optional dependency: only the refresh needs it
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    calls = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        payload = json.dumps([{"id": i, "text": t} for i, t in enumerate(batch)], ensure_ascii=False)
        response = client.messages.create(
            model=MODEL, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": payload}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if response.stop_reason != "end_turn":
            raise SystemExit(f"intent batch {start // BATCH}: stop_reason={response.stop_reason}")
        items = json.loads(next(b.text for b in response.content if b.type == "text"))["items"]
        if sorted(it["id"] for it in items) != list(range(len(batch))):
            raise SystemExit(f"intent batch {start // BATCH}: ids returned do not match the batch")
        for it in items:
            cache["entries"][key(batch[it["id"]])] = {"label": it["label"], "reason": it["reason"]}
        cache["calls"].append({"model": MODEL, "texts": len(batch),
                               "input_tokens": response.usage.input_tokens,
                               "output_tokens": response.usage.output_tokens})
        calls += 1
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(cache, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8", newline="\n")  # saved per batch, so a failed run keeps its progress
    return calls


def usage(path=CACHE):
    """-> per-model totals of the calls recorded in the cache."""
    out = {}
    for c in load(path)["calls"]:
        m = out.setdefault(c["model"], {"calls": 0, "texts": 0, "input_tokens": 0, "output_tokens": 0})
        for f in ("texts", "input_tokens", "output_tokens"):
            m[f] += c[f]
        m["calls"] += 1
    for model, m in out.items():
        pin, pout = PRICE_PER_MTOK[model]
        m["cost_usd"] = (m["input_tokens"] * pin + m["output_tokens"] * pout) / 1e6
    return out
