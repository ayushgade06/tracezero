"""LLM intent: the combination rule, and the cache refresh against a fake Claude client (no network)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import intent, llm_intent  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        batch = json.loads(kw["messages"][0]["content"])
        items = [{"id": b["id"], "label": "NON_DELETION" if "copy" in b["text"] else "DELETION", "reason": "r"}
                 for b in batch]
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"items": items}))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10))


class LlmIntentTests(unittest.TestCase):
    def test_decide(self):
        D, N, A = intent.DELETION, intent.NON_DELETION, intent.AMBIGUOUS
        self.assertEqual(intent.decide(N, None), N)  # not cached: rule label
        self.assertEqual(intent.decide(N, N), N)
        self.assertEqual(intent.decide(A, N), N)  # Claude settles an ambiguous rule label
        self.assertEqual(intent.decide(N, D), D)  # disagreement is audited as deletion
        self.assertEqual(intent.decide(D, N), D)

    def test_refresh_batches_caches_and_counts_usage(self):
        texts = [f"please delete account {i}" for i in range(llm_intent.BATCH + 5)] + ["send me a copy"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "intent.json"
            client = FakeClient()
            self.assertEqual(llm_intent.refresh(texts + texts, path, client), 2)  # duplicates sent once
            self.assertEqual(llm_intent.refresh(texts, path, client), 0)  # fully cached: no call
            got = llm_intent.labels(texts, path)
            self.assertEqual(got["send me a copy"], "NON_DELETION")
            self.assertEqual(got["please delete account 0"], "DELETION")
            self.assertIsNone(llm_intent.labels(["unseen"], path)["unseen"])
            u = llm_intent.usage(path)[llm_intent.MODEL]
            self.assertEqual((u["calls"], u["texts"], u["input_tokens"], u["output_tokens"]), (2, len(texts), 200, 20))


if __name__ == "__main__":
    unittest.main()
