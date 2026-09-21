import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_payload import compact_json, research_payload
from test_research import brief, evidence
from trading_agent import TradingAgent, Settings, demo_data, initial_state, ResearchCandidate, variant_hypothesis


def unpack(text):
    packed = json.loads(text)
    if packed.get("format") != "shared-json-v1":
        return packed
    marker = packed["reference_key"]

    def expand(value):
        if isinstance(value, dict):
            if set(value) == {marker}:
                return expand(packed["shared"][value[marker]])
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    return expand(packed["data"])


class PromptPayloadTests(unittest.TestCase):
    def test_nested_memory_is_lossless_and_smaller_without_mutation(self):
        item = {"hypothesis": variant_hypothesis(0).model_dump(), "brief": brief().model_dump(),
                "training_results": {"net_return": .12345678901234567},
                "validation_feedback": {"reasons": ["Operaciones insuficientes"]}}
        payload = {"memory": [copy.deepcopy(item) for _ in range(12)], "evidence": evidence()}
        original = copy.deepcopy(payload)
        packed = research_payload(payload)
        self.assertEqual(unpack(packed), original)
        self.assertEqual(payload, original)
        self.assertLess(len(packed), len(compact_json(payload)))

    def test_small_payload_and_unicode_preserve_exact_values(self):
        value = {"a": "Investigación y señal", "b": [False, True, None, 0, .12345678901234567]}
        self.assertEqual(unpack(research_payload(value)), value)
        self.assertNotIn("\\u", research_payload(value))
        self.assertLessEqual(len(research_payload(value)), len(compact_json(value)))

    def test_reference_key_collision_and_nested_duplicates(self):
        shared = {"$shared": "not a reference", "large": "x" * 400}
        value = {"items": [{"nested": shared, "other": shared}] * 5}
        self.assertEqual(unpack(research_payload(value)), value)

    def test_prompt_preserves_context_evidence_and_memory_and_disk_stays_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            agent.demo = False
            context = agent.research_context()
            history = [{"brief": brief().model_dump(), "hypothesis": variant_hypothesis(1).model_dump()}] * 12
            candidate = ResearchCandidate(brief=brief(), hypothesis=variant_hypothesis(0))
            with patch.object(agent, "recent_research", return_value=history), \
                 patch.object(agent, "search_literature", return_value=evidence()) as search, \
                 patch.object(agent, "call_model", return_value=candidate) as model:
                result = agent.researcher_node(initial_state())
            self.assertEqual(result["status"], "RESEARCHING")
            search.assert_called_once_with(context, history)
            model.assert_called_once()
            payload = model.call_args.args[1].split(" Datos (context=IS, evidence=dossier, memory=historial): ")[1]
            self.assertEqual(unpack(payload), {"context": context, "evidence": evidence(), "memory": history})
            stored = json.loads((agent.output/"research/attempt_01.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["training_context"], context)
            self.assertEqual(stored["evidence"], evidence())


if __name__ == "__main__":
    unittest.main()
