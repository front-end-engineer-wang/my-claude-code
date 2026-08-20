import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from coding_assistant import llm


class CacheCompatibilityError(Exception):
    status_code = 400


class LlmCachingTests(unittest.TestCase):
    def setUp(self):
        llm.reset_cache_support_for_tests()

    @staticmethod
    def response(cache_read=0, cache_creation=0):
        return SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=20,
                output_tokens=5,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            ),
        )

    def test_cache_control_is_added_to_stable_system_block(self):
        create = Mock(return_value=self.response(cache_creation=50))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", create),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
            patch.object(llm, "PROMPT_CACHE_ENABLED", True),
        ):
            llm.call_message(
                model="test-model", stable_system="stable",
                semi_stable_system="workspace", dynamic_system="memory",
                messages=[{"role": "user", "content": "hello"}],
                tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
                max_tokens=100, call_type="agent", session_id="s1",
            )
            kwargs = create.call_args.kwargs
            self.assertIsInstance(kwargs["system"], list)
            self.assertEqual(kwargs["system"][0]["cache_control"]["type"], "ephemeral")
            metric = json.loads(next(Path(temp).glob("*.jsonl")).read_text(encoding="utf-8"))
            self.assertEqual(metric["cache_creation_input_tokens"], 50)
            self.assertEqual(metric["call_type"], "agent")

    def test_unsupported_cache_control_falls_back_and_stays_disabled(self):
        create = Mock(side_effect=[
            CacheCompatibilityError("unknown cache_control field"),
            self.response(), self.response(),
        ])
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", create),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
            patch.object(llm, "PROMPT_CACHE_ENABLED", True),
        ):
            for _ in range(2):
                llm.call_message(
                    model="test-model", stable_system="stable", messages=[],
                    max_tokens=20, call_type="agent",
                )
        self.assertEqual(create.call_count, 3)
        self.assertIsInstance(create.call_args_list[1].kwargs["system"], str)
        self.assertIsInstance(create.call_args_list[2].kwargs["system"], str)

    def test_unsupported_ttl_is_dropped_without_disabling_cache(self):
        create = Mock(side_effect=[
            CacheCompatibilityError("unknown ttl field"),
            self.response(cache_creation=30), self.response(cache_read=30),
        ])
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", create),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
            patch.object(llm, "PROMPT_CACHE_ENABLED", True),
            patch.object(llm, "PROMPT_CACHE_TTL", "5m"),
        ):
            for _ in range(2):
                llm.call_message(
                    model="test-model", stable_system="stable", messages=[],
                    max_tokens=20, call_type="agent",
                )

        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            create.call_args_list[0].kwargs["system"][0]["cache_control"]["ttl"],
            "5m",
        )
        for call in create.call_args_list[1:]:
            self.assertIsInstance(call.kwargs["system"], list)
            self.assertNotIn("ttl", call.kwargs["system"][0]["cache_control"])

    def test_usage_metrics_recognize_cache_hit(self):
        metrics = llm.usage_metrics(self.response(cache_read=120))
        self.assertTrue(metrics["cache_hit"])
        self.assertEqual(metrics["estimated_saved_input_tokens"], 120)

    def test_prefix_hash_ignores_dynamic_sections_but_changes_tools(self):
        base = llm.prompt_prefix_hash("m", "stable", [])
        same = llm.prompt_prefix_hash("m", "stable", [])
        changed = llm.prompt_prefix_hash(
            "m", "stable", [{"name": "bash", "input_schema": {"type": "object"}}]
        )
        self.assertEqual(base, same)
        self.assertNotEqual(base, changed)

    def test_retry_can_switch_model_provider_and_records_final_model(self):
        selected = {"model": "primary-model"}
        create = Mock(side_effect=[
            RuntimeError("529 overloaded"), self.response(cache_read=25),
        ])

        def retry(invoke):
            try:
                return invoke()
            except RuntimeError:
                selected["model"] = "fallback-model"
                return invoke()

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", create),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
        ):
            llm.call_message(
                model=lambda: selected["model"], stable_system="stable",
                messages=[], max_tokens=20, call_type="agent", retry=retry,
            )
            metric = json.loads(
                next(Path(temp).glob("*.jsonl")).read_text(encoding="utf-8")
            )

        self.assertEqual(create.call_args_list[0].kwargs["model"], "primary-model")
        self.assertEqual(create.call_args_list[1].kwargs["model"], "fallback-model")
        self.assertEqual(metric["model"], "fallback-model")
        self.assertEqual(metric["attempt_count"], 2)
        self.assertEqual(
            metric["prompt_prefix_hash"],
            llm.prompt_prefix_hash("fallback-model", "stable", []),
        )

    def test_llm_context_propagates_session_and_trace_to_nested_calls(self):
        events = []
        create = Mock(return_value=self.response(cache_read=40))

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", create),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
            llm.llm_context("parent-session", lambda kind, payload: events.append(
                (kind, payload)
            )),
        ):
            llm.call_message(
                model="test-model", stable_system="subagent stable",
                messages=[{"role": "user", "content": "nested work"}],
                max_tokens=20, call_type="subagent",
            )

            metric = json.loads(
                next(Path(temp).glob("*.jsonl")).read_text(encoding="utf-8")
            )

        self.assertEqual(metric["session_id"], "parent-session")
        usage_events = [payload for kind, payload in events if kind == "llm_usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0]["session_id"], "parent-session")
        self.assertEqual(usage_events[0]["call_type"], "subagent")
        self.assertTrue(usage_events[0]["cache_hit"])

if __name__ == "__main__":
    unittest.main()
