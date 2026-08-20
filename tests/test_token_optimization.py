import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from coding_assistant import compaction, llm
from coding_assistant.mcp import connect_mcp, mcp_clients
from coding_assistant.registry import assemble_tool_pool
from coding_assistant.skills import assemble_system_prompt_parts
from coding_assistant.storage import ConversationStore


class PromptStructureTests(unittest.TestCase):
    def test_dynamic_context_does_not_change_stable_prefix(self):
        first = assemble_system_prompt_parts({
            "memories": "memory one", "active_teammates": ["a"]
        }, ["bash"])
        second = assemble_system_prompt_parts({
            "memories": "memory two", "active_teammates": ["b"]
        }, ["bash"])
        self.assertEqual(first["stable"], second["stable"])
        self.assertNotEqual(first["dynamic"], second["dynamic"])
        self.assertNotIn("Current time", first["stable"] + first["semi_stable"])


class ToolSelectionTests(unittest.TestCase):
    def tearDown(self):
        mcp_clients.clear()

    @staticmethod
    def names(intent):
        schemas, _ = assemble_tool_pool(intent)
        return [schema["name"] for schema in schemas]

    def test_default_only_exposes_core_tools(self):
        names = self.names("fix this Python function")
        self.assertIn("read_file", names)
        self.assertIn("connect_mcp", names)
        self.assertNotIn("create_task", names)
        self.assertNotIn("schedule_cron", names)
        self.assertNotIn("spawn_teammate", names)

    def test_keywords_enable_task_cron_and_team_groups(self):
        self.assertIn("create_task", self.names("创建任务计划和依赖"))
        self.assertIn("schedule_cron", self.names("schedule a cron job"))
        team = self.names("使用团队并行协作")
        self.assertIn("spawn_teammate", team)
        self.assertIn("create_task", team)

    def test_mcp_tools_require_connected_server_name_in_intent(self):
        connect_mcp("docs")
        self.assertFalse(any(name.startswith("mcp__docs") for name in self.names("fix code")))
        self.assertTrue(any(name.startswith("mcp__docs") for name in self.names("search docs")))

    def test_schema_order_is_stable(self):
        self.assertEqual(self.names("团队并行"), self.names("团队并行"))


class CompactionTests(unittest.TestCase):
    def test_duplicate_consumed_tool_results_are_replaced(self):
        large = "same output " * 30
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "read_file", "input": {"path": "a.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": large}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "2", "name": "read_file", "input": {"path": "a.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "2", "content": large}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        compaction.deduplicate_tool_results(messages)
        self.assertEqual(messages[1]["content"][0]["content"], large)
        self.assertIn("Duplicate read_file result omitted", messages[3]["content"][0]["content"])

    def test_summary_compaction_preserves_latest_complete_tool_round(self):
        latest_assistant = {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": "latest", "name": "bash",
                "input": {"command": "pytest"},
            }],
        }
        latest_result = {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "latest",
                "content": "1 failed",
            }],
        }
        messages = [
            {"role": "user", "content": "old request"},
            latest_assistant,
            latest_result,
        ]
        with (
            patch.object(compaction, "write_transcript", return_value=Path("trace")),
            patch.object(compaction, "summarize_history", return_value="old state"),
        ):
            compacted = compaction.compact_history(messages, "current request")

        self.assertIn("Authoritative request:\ncurrent request", compacted[0]["content"])
        self.assertIs(compacted[-2], latest_assistant)
        self.assertIs(compacted[-1], latest_result)

    def test_summary_failure_keeps_recent_tool_round_and_continues(self):
        messages = [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "x", "name": "read_file",
                "input": {"path": "a.py"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "x", "content": "data",
            }]},
        ]
        with (
            patch.object(compaction, "write_transcript", return_value=Path("trace")),
            patch.object(compaction, "summarize_history", side_effect=RuntimeError("offline")),
        ):
            compacted = compaction.compact_history(messages, "keep going")

        self.assertEqual(len(compacted), 3)
        self.assertIn("summary failed", compacted[0]["content"])
        self.assertTrue(compaction.message_has_tool_use(compacted[-2]))
        self.assertTrue(compaction.is_tool_result_message(compacted[-1]))

    def test_proactive_thresholds_progress_without_splitting_latest_pair(self):
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "result"}]},
        ]
        with (
            patch.object(compaction, "estimate_size", side_effect=[80, 80]),
            patch.object(compaction, "micro_compact", wraps=compaction.micro_compact) as micro,
            patch.object(compaction, "CONTEXT_ACTIVE_THRESHOLD", 0.7),
            patch.object(compaction, "CONTEXT_COMPACT_THRESHOLD", 0.85),
            patch.object(compaction, "CONTEXT_SUMMARY_THRESHOLD", 0.95),
        ):
            compaction.proactive_compact(messages, "start", 100)
        micro.assert_called_once()
        self.assertTrue(compaction.message_has_tool_use(messages[-2]))
        self.assertTrue(compaction.is_tool_result_message(messages[-1]))


class CostRegressionTests(unittest.TestCase):
    def tearDown(self):
        mcp_clients.clear()
        llm.reset_cache_support_for_tests()

    def test_mocked_ten_round_session_meets_cache_and_schema_targets(self):
        core_tools, _ = assemble_tool_pool("fix code")
        all_tools, _ = assemble_tool_pool("", include_all=True)
        connect_mcp("docs")
        docs_tools, _ = assemble_tool_pool("search docs")

        seen_prefixes = set()
        transient_failure = {"pending": True}

        def response(cache_read=0, cache_creation=0):
            return SimpleNamespace(
                content=[], stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=100, output_tokens=10,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read,
                ),
            )

        def create(**kwargs):
            if kwargs["messages"] and kwargs["messages"][-1].get("retry_test"):
                if transient_failure["pending"]:
                    transient_failure["pending"] = False
                    raise RuntimeError("transient gateway failure")
            system = kwargs["system"]
            stable = system[0]["text"] if isinstance(system, list) else system
            key = llm.prompt_prefix_hash(kwargs["model"], stable, kwargs["tools"])
            prefix_tokens = max(
                1, llm.estimate_tokens(stable) + llm.estimate_tokens(kwargs["tools"])
            )
            if key in seen_prefixes:
                return response(cache_read=prefix_tokens)
            seen_prefixes.add(key)
            return response(cache_creation=prefix_tokens)

        def retry_once(invoke):
            try:
                return invoke()
            except RuntimeError:
                return invoke()

        file_result = {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "read-1",
                "content": "file content " * 100,
            }],
        }
        search_result = {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "search-1",
                "content": "match a.py:10\n" * 50,
            }],
        }
        error_result = {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "bash-1",
                "content": "Error: test command failed",
            }],
            "retry_test": True,
        }
        rounds = [
            ("agent", "agent stable", core_tools, [{"role": "user", "content": "fix"}], None),
            ("agent", "agent stable", core_tools, [file_result], None),
            ("agent", "agent stable", core_tools, [search_result], None),
            ("agent", "agent stable", core_tools, [error_result], retry_once),
            ("agent", "agent stable", core_tools, [file_result, search_result], None),
            ("agent", "agent stable", core_tools, [{"role": "user", "content": "continue"}], None),
            ("agent", "agent stable", docs_tools, [{"role": "user", "content": "search docs"}], None),
            ("agent", "agent stable", docs_tools, [{"role": "user", "content": "more docs"}], None),
            ("subagent", "subagent stable", core_tools, [{"role": "user", "content": "delegated"}], None),
            ("teammate", "teammate stable", core_tools, [{"role": "user", "content": "team task"}], None),
        ]

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(llm.client.messages, "create", Mock(side_effect=create)),
            patch.object(llm, "TOKEN_USAGE_DIR", Path(temp)),
            patch.object(llm, "PROMPT_CACHE_ENABLED", True),
        ):
            for call_type, stable, tools, messages, retry in rounds:
                llm.call_message(
                    model="test-model", stable_system=stable,
                    messages=messages, tools=tools, max_tokens=100,
                    call_type=call_type, session_id="cost-regression", retry=retry,
                )
            metrics = [json.loads(line) for line in
                       next(Path(temp).glob("*.jsonl")).read_text(
                           encoding="utf-8").splitlines()]

        cache_hit_rate = sum(metric["cache_hit"] for metric in metrics) / len(metrics)
        uncached = sum(metric["uncached_equivalent_input_tokens"] for metric in metrics)
        paid_uncached_input = sum(
            metric["input_tokens"] + metric["cache_creation_input_tokens"]
            for metric in metrics
        )
        input_reduction = 1 - (paid_uncached_input / uncached)
        schema_reduction = 1 - (
            llm.estimate_tokens(core_tools) / llm.estimate_tokens(all_tools)
        )

        self.assertEqual(len(metrics), 10)
        self.assertGreaterEqual(cache_hit_rate, 0.60)
        self.assertGreaterEqual(input_reduction, 0.30)
        self.assertGreaterEqual(schema_reduction, 0.40)
        self.assertEqual(max(metric["attempt_count"] for metric in metrics), 2)
        self.assertIn("subagent", {metric["call_type"] for metric in metrics})
        self.assertIn("teammate", {metric["call_type"] for metric in metrics})
        self.assertTrue(any(name.startswith("mcp__docs") for name in
                            metrics[6]["tool_names"]))


class UsageStorageTests(unittest.TestCase):
    def test_llm_usage_events_update_conversation_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConversationStore(Path(temp))
            record = store.create()
            store.append_debug(record["id"], "llm_usage", {
                "input_tokens": 10, "output_tokens": 2,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40, "cache_hit": True,
                "estimated_saved_input_tokens": 40, "attempt_count": 3,
            })
            usage = store.get(record["id"])["token_usage"]
        self.assertEqual(usage["request_count"], 3)
        self.assertEqual(usage["call_count"], 1)
        self.assertEqual(usage["cache_read_input_tokens"], 40)
        self.assertEqual(usage["cache_hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
