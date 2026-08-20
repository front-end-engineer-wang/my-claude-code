"""Integrated model loop and automatic event wakeups."""

import threading
import time

from .background import (
    collect_background_results, has_pending_background, should_run_background,
    start_background_task,
)
from .compaction import (
    RecoveryState, block_type, compact_history, is_prompt_too_long_error,
    proactive_compact, reactive_compact, tool_result_budget, with_retry,
)
from .config import (
    CONTEXT_LIMIT, CONTINUATION_PROMPT, DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES, terminal_print,
)
from .cron import (
    CronJob, acknowledge_cron_jobs, consume_cron_queue, cron_lock, cron_queue,
    restore_cron_jobs,
)
from .hooks import trigger_hooks
from .memory import MEMORY_RUNTIME
from .mcp import mcp_clients
from .llm import call_message, llm_context
from .registry import assemble_tool_pool
from .skills import assemble_system_prompt_parts
from .subagents import has_tool_use
from .tasks import release_completed_assignment
from .teams import active_teammates, consume_lead_inbox, format_team_events
from .tools import call_tool_handler


def _emit_trace(callback, event_type: str, payload: dict):
    """Tracing must never change agent behavior if the UI recorder fails."""
    if callback is None:
        return
    try:
        callback(event_type, payload)
    except Exception as exc:
        terminal_print(f"[trace] recorder error: {type(exc).__name__}: {exc}")

# -- Context --


def update_context(context: dict, messages: list) -> dict:
    return {
        "memory_catalog": MEMORY_RUNTIME.read_memory_index(),
        "memories": MEMORY_RUNTIME.load_memories(messages),
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }


def remember_after_turn(messages: list) -> None:
    if MEMORY_RUNTIME.extract_memories(messages):
        MEMORY_RUNTIME.consolidate_memories()


# -- Agent Loop --

rounds_since_todo = 0
agent_lock = threading.Lock()


def prepare_context(messages: list, active_request: str, *,
                    session_id: str | None = None, trace_callback=None) -> list:
    # Every LLM turn enters through the same context budget pipeline.
    messages[:] = tool_result_budget(messages)
    messages[:] = proactive_compact(
        messages, active_request, CONTEXT_LIMIT, session_id=session_id,
        trace_callback=trace_callback,
    )
    return messages

def tool_intent_text(active_request: str, messages: list) -> str:
    """Build a bounded local classifier input from the request and recent events."""
    recent = []
    for message in messages[-8:]:
        content = str(message.get("content", ""))
        recent.append(content[:2000])
    return "\n".join([str(active_request), *recent])


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    notes = collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int, trace_callback=None,
             session_id: str | None = None):
    tool_names = [tool.get("name", "") for tool in tools]
    prompt = assemble_system_prompt_parts(context, tool_names)
    return call_message(
        model=lambda: state.current_model,
        stable_system=prompt["stable"],
        semi_stable_system=prompt["semi_stable"],
        dynamic_system=prompt["dynamic"],
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        call_type="agent",
        session_id=session_id,
        retry=lambda invoke: with_retry(invoke, state),
        trace_callback=trace_callback,
    )

def _agent_loop_impl(messages: list, context: dict, active_request: str,
                     trace_callback=None, session_id: str | None = None):
    global rounds_since_todo
    tools, handlers = assemble_tool_pool(tool_intent_text(active_request, messages))
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    unacknowledged_cron_jobs: list[CronJob] = []
    while True:
        # One cycle: inject scheduled/background work, prepare context, call
        # the model, execute tool_use blocks, append tool_results, repeat.
        fired = consume_cron_queue()
        unacknowledged_cron_jobs.extend(fired)
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        if fired:
            scheduled_requests = "\n".join(
                f"Run scheduled task: {job.prompt}" for job in fired)
            active_request = f"{active_request}\n{scheduled_requests}".strip()

        inject_background_notifications(messages)

        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        prepare_context(messages, active_request, session_id=session_id,
                        trace_callback=trace_callback)
        context = update_context(context, messages)
        tools, handlers = assemble_tool_pool(tool_intent_text(active_request, messages))

        try:
            response = call_llm(messages, context, tools, state, max_tokens,
                                trace_callback, session_id)
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(
                    messages, active_request, session_id=session_id,
                    trace_callback=trace_callback)
                state.has_attempted_reactive_compact = True
                continue
            restore_cron_jobs(unacknowledged_cron_jobs)
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            release_completed_assignment("agent")
            return

        acknowledge_cron_jobs(unacknowledged_cron_jobs)
        unacknowledged_cron_jobs.clear()

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            release_completed_assignment("agent")
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            trigger_hooks("Stop", messages)
            remember_after_turn(messages)
            release_completed_assignment("agent")
            return

        results = []
        compact_requested = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if block.name == "compact":
                _emit_trace(trace_callback, "tool_call", {"name": block.name, "id": block.id, "input": block.input})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compaction requested. This completed turn will be summarized.]",
                })
                compact_requested = True
                _emit_trace(trace_callback, "tools_result", {"tool_use_id": block.id, "name": block.name, "content": "[Compaction requested. This completed turn will be summarized.]"})
                continue

            _emit_trace(trace_callback, "tool_call", {"name": block.name, "id": block.id, "input": block.input})
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                _emit_trace(trace_callback, "tools_result", {"tool_use_id": block.id, "name": block.name, "content": str(blocked), "blocked": True})
                continue

            if should_run_background(block.name, block.input):
                try:
                    bg_id = start_background_task(block, handlers)
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                except Exception as exc:
                    output = (f"Error: Failed to start background task: "
                              f"{type(exc).__name__}: {exc}")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                _emit_trace(trace_callback, "tools_result", {"tool_use_id": block.id, "name": block.name, "content": output, "background": True})
                continue

            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
            _emit_trace(trace_callback, "tools_result", {"tool_use_id": block.id, "name": block.name, "input": block.input, "content": output})

        messages.append({"role": "user", "content": build_user_content(results)})
        if compact_requested:
            messages[:] = compact_history(
                messages, active_request, session_id=session_id,
                trace_callback=trace_callback)


def agent_loop(messages: list, context: dict, active_request: str,
               trace_callback=None, session_id: str | None = None):
    with llm_context(session_id, trace_callback):
        return _agent_loop_impl(
            messages, context, active_request, trace_callback, session_id
        )


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if block_type(block) == "text":
                terminal_print(block["text"] if isinstance(block, dict) else block.text)


def async_event_loop(history: list, context: dict, session_state: dict):
    while True:
        time.sleep(1)
        with agent_lock:
            with cron_lock:
                fired = list(cron_queue)
            inbox = consume_lead_inbox(route_protocol=True)
            if not fired and not inbox and not has_pending_background():
                continue
            turn_start = len(history)
            scheduled_requests = []
            for job in fired:
                scheduled_requests.append(f"Run scheduled task: {job.prompt}")
                terminal_print(
                    f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            if inbox:
                history.append({"role": "user",
                                "content": format_team_events(inbox)})
                terminal_print(
                    f"  \033[33m[team auto] {len(inbox)} events\033[0m")
            active_request = (
                "\n".join(scheduled_requests)
                if scheduled_requests
                else session_state["active_user_request"]
            )
            agent_loop(history, context, active_request)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)
