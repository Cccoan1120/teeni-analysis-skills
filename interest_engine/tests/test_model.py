import json
from unittest.mock import patch

from django.test import SimpleTestCase

from interest_engine.model import QwenCandidateEnricher
from interest_engine.registry import EntitySubtype


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, maximum):
        content = json.dumps({
            "items": [{
                "id": "C001",
                "accepted": True,
                "canonicalName": "测试实体",
                "entityType": "其他",
                "entitySubtype": "其他热梗",
                "parentName": "",
                "aliases": [],
                "confidence": "中",
            }],
        }, ensure_ascii=False)
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


class QwenCandidateEnricherTests(SimpleTestCase):
    @patch("interest_engine.model.urlopen", return_value=_Response())
    def test_prompt_enumerates_every_allowed_subtype(self, mocked_urlopen):
        enricher = QwenCandidateEnricher("https://qwen.example/v1", "secret", "Qwen-test")
        result = enricher.enrich([{
            "phrase": "测试实体",
            "normalizedPhrase": "测试实体",
            "users": 5,
            "sessions": 8,
            "examples": [],
        }])
        request = mocked_urlopen.call_args.args[0]
        prompt = json.loads(request.data.decode("utf-8"))["messages"][0]["content"]
        for subtype in EntitySubtype:
            self.assertIn(subtype.value, prompt)
        self.assertEqual(result[0]["suggestedSubtype"], "其他热梗")
