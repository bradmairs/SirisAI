"""Cortex (milestone 33): the single orchestration path a chat turn goes
through -- request -> context -> route -> plan/act -> tool execution ->
persist -> response.

A capstone, not a new subsystem: every piece already existed --
`ContextEngine` (milestone 16), the model router (milestone 20-ish), the
`Planner` (milestone 24), `SirisAI.run_tool()` (milestone 11). Before this
milestone, `POST /siris/chat` assembled all of that inline -- building
context, picking a model, branching on `use_planner`, executing a tool,
making the follow-up call -- with the HTTP route function itself as the
only thing that ever called through the whole pipeline. `Cortex` names and
owns that pipeline instead, decoupled from FastAPI/pydantic (it returns
plain dataclasses, not response models) so it's reusable from anywhere,
not just one route handler. The route in app/api/routes/siris.py is now a
thin adapter: build a `CortexResult`, then shape it into a `ChatResponse`
and raise the right HTTPException if `error` is set.

This is also what makes the LLM client "visibly swappable infrastructure":
callers of Cortex never touch an `LLMClient` directly, `SirisAI.model_router`
decides which one handles a given turn, and Cortex is the only thing that
calls `.generate()` on it.

Persona (a hardening pass, milestone 45): `SIRISAI_PERSONA`, when set, is
prepended as a system message on every turn Cortex drives -- `process()`,
`confirm()`, and the planner loop both feed through -- ahead of context
(16), which stays a separate, `use_context`-gated concern. Empty (the
default) means no persona message at all, the same raw-model-default
behavior as before this milestone existed.

Performance hardening pass: `SirisAI.run_tool()` and `ContextEngine.build()`
are both synchronous, blocking calls (a tool's own HTTP client, `psutil`
sampling, DB queries) -- this process runs a single uvicorn worker/event
loop (see the Dockerfile), so calling either directly from these `async
def` methods would block that one event loop for the call's full
duration, freezing every other concurrent request (another chat turn, a
voice satellite, a proactive check) regardless of what it's doing. Both
are run via `asyncio.to_thread()` instead, so the blocking work happens
on a worker thread and the event loop stays free to handle everything
else meanwhile -- see app/db/session.py's WAL-mode comment for why this
is safe to do concurrently against the same SQLite file.

Automatic memory extraction (a hardening pass, SIRISAI_AUTO_MEMORY_EXTRACTION_ENABLED):
`remember`/`remember_entity_fact`/etc. were already callable by the model
on its own initiative, mid-conversation -- the actual gap was structural,
not permissions. A normal (non-planner) turn gets exactly one tool call,
so a model already spending it on the user's actual request has no room
left to also save a memory in the same turn; "self-learning" would mean
the model choosing memory-keeping over helping, which it correctly never
does. `process()`/`confirm()` both wrap their real work and, once a real
answer comes back (`response` set, no `error`), run `_maybe_extract_memory()`
-- a *separate* small `Planner` run (recall existing memory, then decide
whether to save something new, or nothing), off the "fast" model role
when one's configured (app/llm/router.py) to keep the added cost down,
and using `Planner`'s own `trigger_template` so its audit trail honestly
reads "automatic memory extraction," never as if the user's own request
drove it. Its steps are never added to the visible conversation transcript
(`Planner.run()` itself never touches conversation persistence -- only
`Cortex._run_planned()` does, which this bypasses on purpose) -- an
internal side-process, not something that should show up replayed into
a later turn's messages. Best-effort throughout: any failure here is
swallowed and logged, never the reason a chat turn fails.
"""

import asyncio
import re
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core import taint
from app.cortex import quick, timings, trial
from app.notify.confirmation import notify_confirmation_needed
from app.llm.streaming import EventCallback, generate_with_events
from app.planning.planner import Planner
from app.plugins.base import ConfirmationRequired, ToolPermission

if TYPE_CHECKING:
    from app.core.siris import SirisAI

logger = logging.getLogger(__name__)

# Automatic memory extraction (a hardening pass): the only tools this
# internal pass is ever offered -- durable-fact storage/lookup, not
# log_event/recall_events (things that *happened*, a different concept
# from a fact worth keeping) and definitely nothing else this codebase
# can do. Restricting the toolset, not just the prompt, is the actual
# safety boundary: even a misbehaving extraction pass can't call
# anything but these six.
_MEMORY_EXTRACTION_TOOL_NAMES = {
    "recall",
    "remember",
    "recall_entity_facts",
    "remember_entity_fact",
    "recall_entity_relationships",
    "remember_entity_relationship",
}
_MEMORY_EXTRACTION_MAX_STEPS = 3
_MEMORY_EXTRACTION_SYSTEM_PROMPT = (
    "You just handled one exchange in an ongoing conversation. Decide whether the USER told you "
    "something worth saving to long-term memory for future conversations.\n\n"
    "Save something only if the user themselves stated it and it will still be true and useful weeks "
    "from now: a preference, a fact about them, their home or setup, or a named person/service/"
    "device/project. Save it in one short sentence.\n\n"
    "Never save: anything that came from a tool result, system status or the assistant's own reply "
    "(device lists, network details, CPU or disk figures, weather); what SirisAI is or can do; goals, "
    "routines or tasks that were just created; a one-off request or command; a current state or a "
    "passing remark (\"I'm going to bed\", \"turn it on\"); greetings and small talk. If unsure, save "
    "nothing.\n\n"
    "Before saving anything, use recall / recall_entity_facts / recall_entity_relationships to "
    "check it isn't already known -- don't save a duplicate of something already remembered. If "
    "nothing qualifies, don't call any tool at all -- just say so."
)

# A cheap pre-check before spending a whole extra LLM call on extraction:
# the user's message has to actually state something about themselves or
# their setup. Found live: extraction ran after almost every turn, re-saved
# the same fact (deduped, but still a wasted call) and stored things like the
# UniFi device list and "heading to bed" as long-term memories.
_STATEMENT_RE = re.compile(
    r"\b(i am|i'm|i\u2019m|i've|i\u2019ve|i have|i like|i love|i hate|i prefer|i work|i live|i use|i own|"
    r"i moved|i bought|i got|my [a-z' ]{1,30} (is|are|was|were)|remember (that|this)|call me|"
    r"from now on|always|never|we (are|have|use)|our [a-z' ]{1,30} (is|are))\b",
    re.IGNORECASE,
)


def _worth_extracting(user_text: str) -> bool:
    text = user_text.strip()
    return bool(text) and not text.endswith("?") and _STATEMENT_RE.search(text) is not None

# Follow-up synthesis nudge (a hardening pass, live-hit 2026-09-06): the
# non-planner path offers exactly one tool call per turn, so a compound
# question ("what routines AND goals do you have?") gets real data for
# only the part that tool covered -- confirmed live, a model asked this
# called list_routines, then confidently invented an answer about goals
# it never checked ("you have not set up any explicit, named goals"),
# rather than saying it hadn't looked. This is appended as the last
# message before the follow-up call in both _run_single_tool branches
# (success and tool-failure) -- closest to generation, where it has the
# most influence, not buried earlier in `messages`.
_SYNTHESIS_HONESTY_NUDGE = (
    "You have the result of exactly one tool call. Answer only the part of the user's question "
    "that tool result actually covers. For any other part of their question, you have not "
    "looked -- do not answer it at all, do not guess, and do not speculate about why "
    "something might be true or absent. Instead say plainly that you have not checked that "
    "part yet."
)


@dataclass
class ToolCallOutcome:
    name: str
    arguments: dict[str, Any]
    result: Any


# Tools whose results are personal health data (SIRISAI_HEALTH_PRIVATE).
PRIVATE_TOOLS = frozenset({"health_summary", "health_overview", "health_insights", "health_goals", "health_history", "gym_progress", "gym_sessions", "gym_compare", "gym_plan_week", "health_sleep_factors", "gym_month_report", "health_explain"})


def _local_client(siris: "SirisAI") -> Any:
    """A model that runs on this machine, if there is one: the general model
    when it's Ollama, else the fast one when that's Ollama."""
    if siris.settings.llm_provider == "ollama":
        return siris.llm
    if siris.settings.fast_llm_provider == "ollama":
        return getattr(siris.model_router, "_fast", None)
    return None


def _is_local(siris: "SirisAI", client: Any) -> bool:
    return client is None or siris.settings.llm_provider == "ollama" and client is siris.llm or (
        siris.settings.fast_llm_provider == "ollama" and client is getattr(siris.model_router, "_fast", None)
    )


def _health_text(siris: "SirisAI") -> str:
    from app.health import overview

    return overview.text(overview.build(siris))


@dataclass
class CortexResult:
    conversation_id: str
    # Final natural-language answer, if one was produced.
    response: str | None = None
    # Set when exactly one tool ran this turn (the non-planner path).
    tool_call: ToolCallOutcome | None = None
    # Set when the planner ran -- every step it took, in order. Empty list
    # (not None) when the planner ran but needed no tool.
    plan: list[ToolCallOutcome] = field(default_factory=list)
    # Set instead of response/tool_call when a CONFIRM_REQUIRED tool was
    # chosen -- nothing ran. Re-submit via Cortex.confirm().
    pending_confirmation: tuple[str, dict[str, Any]] | None = None
    model_role: str = "general"
    # Set on failure -- the route layer maps this to an HTTP error. Never
    # raised as an exception: Cortex's own contract is "always return a
    # result", the same way run_tool()'s callers already have to handle
    # ConfirmationRequired/PermissionError/Exception as outcomes, not just
    # the model-choosing-a-tool case.
    error: str | None = None
    permission_denied: bool = False
    # Whether this turn ran through the planner (multi-step). The web UI
    # needs it to send `use_planner` back on /chat/confirm.
    used_planner: bool = False
    # Milliseconds per stage of this turn (app/cortex/timings.py).
    timings: dict[str, float] = field(default_factory=dict)


# Phrases that claim a state change happened. Used by
# Cortex._check_action_claims: if the text says one of these but every tool
# that ran only read data, the answer is claiming something it didn't do
# (found live: "Memory entry confirmed and removed" after only `recall`).
# How an attached image's description is stored inside the user's message.
_IMAGE_NOTE_RE = re.compile(r"\[\[image: (.*?)\]\]", re.DOTALL)

_ACTION_CLAIM_RE = re.compile(
    r"\b(removed|deleted|forgotten|forgot|erased)\b|"
    r"\bturned (on|off)\b|"
    r"\b(i've|i have|has been|have been|is now) (saved|stored|created|sent|logged|recorded|set up|added)\b",
    re.IGNORECASE,
)


class Cortex:
    """One request -> response orchestration path for a chat turn. See
    module docstring."""

    def __init__(self, max_plan_steps: int = 5, on_event: EventCallback | None = None) -> None:
        self.max_plan_steps = max_plan_steps
        # Optional live-progress callback for POST /siris/chat/stream: called
        # with small dicts ({"type": "status" | "thinking" | "content" |
        # "tool_start" | "tool_end" | "final", ...}) as the turn progresses.
        # None (default) = silent, and every code path below behaves exactly
        # as it did before this existed.
        self._listener = on_event
        self.on_event: EventCallback | None = self._relay if on_event is not None else None
        # How long each part of the current turn took (app/cortex/timings.py).
        self.timer = timings.TurnTimer()

    async def _relay(self, event: dict[str, Any]) -> None:
        if event.get("type") == "content" and event.get("delta"):
            self.timer.mark("first_word")
        assert self._listener is not None
        await self._listener(event)

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            await self.on_event(event)

    async def process(
        self,
        siris: "SirisAI",
        conversation_id: str,
        prompt: str,
        *,
        use_context: bool = True,
        use_tools: bool = True,
        use_planner: bool = False,
        model_role: str | None = None,
        images: list[bytes] | None = None,
    ) -> CortexResult:
        """request -> context -> route -> plan/act -> response, for a new
        user turn. Persists the user's prompt immediately, and whatever
        tool/assistant messages the rest of the pipeline produces. Once a
        real answer comes back, runs automatic memory extraction (see
        module docstring) as a side effect before returning it."""
        self.timer = timings.TurnTimer()
        result = await self._process_inner(
            siris,
            conversation_id,
            prompt,
            use_context=use_context,
            use_tools=use_tools,
            use_planner=use_planner,
            model_role=model_role,
            images=images,
        )
        # The answer is complete here -- tell a streaming client now, so it
        # doesn't sit waiting on memory extraction (a whole extra LLM pass)
        # to see it.
        self._finalize(siris, result)
        self._learn_quick(siris, prompt, result)
        result.timings = timings.record(siris, self.timer)
        await self._emit({"type": "final", "result": result})
        await self._maybe_extract_memory(siris, conversation_id, prompt, result)
        return result

    async def _quick(self, siris: "SirisAI", conversation_id: str, prompt: str) -> CortexResult | None:
        """A plain home command answered without the model (app/cortex/quick.py)."""
        try:
            planned = await asyncio.to_thread(quick.plan, siris, prompt)
        except Exception:  # noqa: BLE001 - fall back to the model
            logger.exception("Quick command check failed")
            return None
        if planned is None:
            return None
        self.timer.path = "quick"
        result = await self._run_single_tool(
            siris, conversation_id, planned.tool, planned.arguments,
            confirmed=False, trigger="chat: quick command (no model needed)",
        )
        if result.tool_call is not None and not result.error:
            reply = planned.reply
            if reply is None:
                output = result.tool_call.result
                reply = ((output or {}).get("message") or (output or {}).get("text")) if isinstance(output, dict) else None
                reply = reply or "Done."
            result.response = reply
            siris.conversation.add_message(conversation_id, role="assistant", content=reply)
            await self._emit({"type": "content", "delta": reply})
        return result

    def _learn_quick(self, siris: "SirisAI", prompt: str, result: CortexResult) -> None:
        """A short request the model answered with one home action: count it
        towards a learned quick command (app/cortex/quick.py)."""
        if self.timer.path == "quick" or result.error or result.pending_confirmation:
            return
        outcomes = [o for o in [*result.plan, *( [result.tool_call] if result.tool_call else [])] if o.name in quick.LEARNABLE]
        if len(outcomes) != 1:
            return
        try:
            quick.learn(siris, prompt, outcomes[0].name, outcomes[0].arguments)
        except Exception:  # noqa: BLE001
            logger.debug("Learning a quick command failed", exc_info=True)

    async def _process_inner(
        self,
        siris: "SirisAI",
        conversation_id: str,
        prompt: str,
        *,
        use_context: bool = True,
        use_tools: bool = True,
        use_planner: bool = False,
        model_role: str | None = None,
        images: list[bytes] | None = None,
    ) -> CortexResult:
        assert siris.llm is not None
        assert siris.model_router is not None

        image_note: str | None = None
        if images:
            await self._emit({"type": "status", "message": "Looking at the image"})
            image_note = await self._describe_images(siris, prompt, images)
        stored_prompt = prompt if image_note is None else f"{prompt}\n\n[[image: {image_note}]]"
        siris.conversation.add_message(conversation_id, role="user", content=stored_prompt)
        if use_tools and not images:
            quick_result = await self._quick(siris, conversation_id, prompt)
            if quick_result is not None:
                return quick_result
        messages = self._load_messages(siris, conversation_id)

        # Choosing tools doesn't depend on the context, so it runs alongside
        # gathering it (4.6) instead of after.
        tools_task: asyncio.Task | None = None
        if use_tools:
            recent_tools = [
                call["function"]["name"]
                for m in messages[-10:]
                for call in (m.get("tool_calls") or [])
            ]

            async def choose_tools() -> list[dict[str, Any]]:
                with self.timer.stage("tools"):
                    return await asyncio.to_thread(siris.available_tool_specs, prompt, recent_tools)

            tools_task = asyncio.create_task(choose_tools())

        # Persona (a hardening pass, milestone 45) comes before context --
        # "who you are" ahead of "here's what's currently relevant" -- and
        # applies regardless of use_context, a separate concern (tone, not
        # situational data).
        #
        # The context block (the time, live system load, memories matched to
        # this message) changes every turn, so it goes just before the newest
        # message rather than at the top (4.6): Ollama reuses its cached work
        # for the longest unchanged start of the prompt, and with the context
        # first that was only ever the persona.
        system_messages: list[dict[str, Any]] = []
        context_messages: list[dict[str, Any]] = []
        context_text: str | None = None
        if siris.settings.persona:
            system_messages.append({"role": "system", "content": siris.settings.persona})
        if use_context:
            await self._emit({"type": "status", "message": "Gathering context"})
            with self.timer.stage("context"):
                context_text = await asyncio.to_thread(siris.context.build, siris, prompt)
            if context_text:
                context_messages.append({"role": "system", "content": f"Context (may or may not be relevant):\n{context_text}"})
        user_text = "\n".join(_IMAGE_NOTE_RE.sub("", m["content"] or "") for m in messages if m.get("role") == "user")
        taint.begin_turn(context_text if use_context else "", f"{user_text}\n{prompt}")
        # What an image says is the image's words, not the user's: text read
        # off a photo (a sign, a screen) mustn't count as something they said.
        for note in _IMAGE_NOTE_RE.findall("\n".join(m["content"] or "" for m in messages if m.get("role") == "user")):
            taint.add_untrusted(note)
        if trial.arm(siris) == "old":  # an "old" day of the speed trial (app/cortex/trial.py)
            messages = [*system_messages, *context_messages, *messages]
        else:
            messages = [*system_messages, *messages[:-1], *context_messages, *messages[-1:]]

        tools = await tools_task if tools_task is not None else None
        resolved_role, llm_client = siris.model_router.pick(siris.llm, prompt, role=model_role)

        if use_planner:
            self.timer.path = "planner"
            with self.timer.stage("model"):
                planned = await self._run_planned(siris, conversation_id, messages, tools, llm_client, resolved_role)
            planned.used_planner = True
            return planned

        await self._emit({"type": "status", "message": "Thinking"})
        try:
            with self.timer.stage("model"):
                result = await generate_with_events(llm_client, messages, self.on_event, tools=tools)
        except Exception:
            logger.exception("LLM generate() failed")
            return CortexResult(conversation_id=conversation_id, model_role=resolved_role, error="LLM backend call failed.")

        if result.tool_calls:
            call = result.tool_calls[0]
            return await self._run_single_tool(
                siris,
                conversation_id,
                call.name,
                call.arguments,
                confirmed=False,
                model_role=resolved_role,
                messages=messages,
                llm_client=llm_client,
                trigger="chat: model chose this tool while responding to the user's request",
            )

        siris.conversation.add_message(conversation_id, role="assistant", content=result.text)
        return CortexResult(conversation_id=conversation_id, response=result.text, model_role=resolved_role)

    async def confirm(
        self, siris: "SirisAI", conversation_id: str, name: str, arguments: dict[str, Any], *, use_planner: bool = False
    ) -> CortexResult:
        """Actually run a tool call a previous result flagged as
        `pending_confirmation` -- the human has now explicitly approved it.
        Once a real answer comes back, runs automatic memory extraction
        (see module docstring, and process()) as a side effect before
        returning it -- a confirmed real action is, if anything, more
        likely to be worth remembering than idle chat.

        `use_planner` is for when that confirmation came from a planner run
        that stopped mid-plan (the original response carried `plan` rather
        than `tool_call` -- see `PlanResult.pending_confirmation`): rather
        than this turn ending after just the one confirmed step, the
        confirmed tool's result is fed into the same kind of loop that
        stopped for it, and `Planner.run()` picks up from there -- further
        tool calls, another `pending_confirmation` if one of those also
        needs approval, or a final answer. `plan` on the result returned is
        the confirmed step followed by whatever the continuation executed
        (this turn's work, same as `process()`'s `plan` is this turn's --
        the earlier steps from before the confirmation are still visible
        via GET /siris/conversations/{id}, not repeated here).

        Without `use_planner` (default), behavior is unchanged from before
        this: the confirmed tool runs, gets one follow-up call for a final
        answer, and the turn ends there -- same as confirming a single
        non-planned tool call always has."""
        result = await self._confirm_inner(siris, conversation_id, name, arguments, use_planner=use_planner)
        self._finalize(siris, result)
        await self._emit({"type": "final", "result": result})
        await self._maybe_extract_memory(siris, conversation_id, f"(user confirmed: {name})", result)
        return result

    async def _confirm_inner(
        self, siris: "SirisAI", conversation_id: str, name: str, arguments: dict[str, Any], *, use_planner: bool = False
    ) -> CortexResult:
        messages = self._load_messages(siris, conversation_id)
        if siris.settings.persona:
            messages = [{"role": "system", "content": siris.settings.persona}, *messages]
        confirmed = await self._run_single_tool(
            siris,
            conversation_id,
            name,
            arguments,
            confirmed=True,
            messages=None if use_planner else messages,
            llm_client=None if use_planner else siris.llm,
            trigger="chat: user explicitly confirmed a previously-proposed action",
        )
        if not use_planner or confirmed.error or confirmed.tool_call is None or siris.llm is None:
            confirmed.used_planner = use_planner
            return confirmed

        continuation_messages = [
            *messages,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
            },
            {"role": "tool", "content": json.dumps(confirmed.tool_call.result)},
        ]
        rest = await self._run_planned(
            siris, conversation_id, continuation_messages, siris.available_tool_specs(), siris.llm, "general"
        )
        return CortexResult(
            conversation_id=conversation_id,
            response=rest.response,
            plan=[confirmed.tool_call, *rest.plan],
            pending_confirmation=rest.pending_confirmation,
            model_role=rest.model_role,
            error=rest.error,
            permission_denied=rest.permission_denied,
            used_planner=True,
        )

    @staticmethod
    async def _describe_images(siris: "SirisAI", prompt: str, images: list[bytes]) -> str:
        """What the vision model sees in the attached images, framed by the
        user's question -- stored with their message (see _IMAGE_NOTE_RE) so
        later turns can still refer to the picture."""
        count = f"{len(images)} images" if len(images) > 1 else "an image"
        if siris.vision is None:
            return f"the user attached {count}, but image understanding isn't set up (SIRISAI_VISION_MODEL), so it couldn't be looked at"
        question = (
            f"The user sent this with the message: {prompt!r}\n"
            "Describe the image in detail, focusing on what's relevant to that message. "
            "Transcribe any visible text exactly."
        )
        try:
            description = await asyncio.to_thread(siris.vision.describe, images, question)
        except Exception:
            logger.exception("Vision model call failed")
            return f"the user attached {count}, but the vision model failed to look at it"
        return f"the user attached {count}; a vision model described it as: {description}"

    @staticmethod
    def _load_messages(siris: "SirisAI", conversation_id: str) -> list[dict[str, Any]]:
        """Replay persisted history as real chat messages -- user/assistant
        text verbatim, and a persisted "tool" row expanded back into the
        same assistant-tool_calls + tool-result pair this module builds
        in-turn (_run_single_tool's follow_up_messages, _run_planned's
        working_messages), so a tool call from a prior turn is real
        context on later turns too, not just whatever text the model
        produced about it. This was milestone 10's explicitly-flagged gap
        ("persisted but not yet replayed... full tool-result round-
        tripping remains future work") -- closing it here."""
        history = siris.conversation.get_messages(conversation_id)
        messages: list[dict[str, Any]] = []
        for m in history:
            if m["role"] == "tool":
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": m["tool_name"], "arguments": m["tool_arguments"] or {}}}],
                    }
                )
                messages.append({"role": "tool", "content": m["content"]})
            elif m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})
        return messages

    async def _run_single_tool(
        self,
        siris: "SirisAI",
        conversation_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
        model_role: str = "general",
        messages: list[dict[str, Any]] | None = None,
        llm_client: Any = None,
        trigger: str,
    ) -> CortexResult:
        """Run one tool through SirisAI.run_tool() (full permission gating
        and audit logging), persist the exchange, and -- when a
        messages/llm_client are given -- feed the result back to the model
        for a final natural-language answer (a second round-trip). Falls
        back to the tool-call-only result if that follow-up call fails or
        produces no text; a raw tool result is already an acceptable
        answer, the follow-up is a best-effort upgrade."""
        await self._emit({"type": "tool_start", "name": name, "arguments": arguments})
        try:
            with self.timer.stage("tool"):
                output = await asyncio.to_thread(
                    siris.run_tool, name, arguments, conversation_id=conversation_id, confirmed=confirmed, trigger=trigger
                )
        except ConfirmationRequired:
            await self._emit({"type": "tool_end", "name": name, "status": "needs_confirmation"})
            return CortexResult(conversation_id=conversation_id, pending_confirmation=(name, arguments), model_role=model_role)
        except PermissionError as exc:
            await self._emit({"type": "tool_end", "name": name, "status": "error"})
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error=str(exc), permission_denied=True)
        except Exception as exc:
            await self._emit({"type": "tool_end", "name": name, "status": "error"})
            logger.exception("Tool '%s' failed", name)
            # Same follow-up-synthesis treatment as a successful result
            # (below) rather than a bare error straight to the client --
            # live-hit case: unifi_list_clients always raises (the cloud
            # API it talks to has no client-listing endpoint at all), and
            # surfacing that as a raw "Tool 'unifi_list_clients' failed."
            # is exactly the kind of implementation detail the plain-
            # English-only chat UI (see app/web/static/index.html) isn't
            # supposed to show. Falls back to the old hard-error shape
            # only if synthesis itself isn't available or fails too.
            error_payload = {"error": str(exc)}
            siris.conversation.add_message(
                conversation_id, role="tool", content=json.dumps(error_payload), tool_name=name, tool_arguments=arguments
            )
            if llm_client is not None and messages is not None:
                follow_up_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                    },
                    {"role": "tool", "content": json.dumps(error_payload)},
                    {"role": "system", "content": _SYNTHESIS_HONESTY_NUDGE},
                ]
                await self._emit({"type": "status", "message": "Writing answer"})
                try:
                    follow_up = await generate_with_events(llm_client, follow_up_messages, self.on_event)
                except Exception:
                    logger.exception("Follow-up LLM call after tool '%s' error failed", name)
                else:
                    if follow_up.text:
                        siris.conversation.add_message(conversation_id, role="assistant", content=follow_up.text)
                        return CortexResult(conversation_id=conversation_id, response=follow_up.text, model_role=model_role)
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error=f"Tool '{name}' failed.")

        await self._emit({"type": "tool_end", "name": name, "status": "ok"})
        if name in PRIVATE_TOOLS and siris.settings.health_private and not _is_local(siris, llm_client):
            return await self._private_answer(siris, conversation_id, name, arguments, output, messages, model_role)
        siris.conversation.add_message(
            conversation_id, role="tool", content=json.dumps(output), tool_name=name, tool_arguments=arguments
        )
        tool_call = ToolCallOutcome(name=name, arguments=arguments, result=output)

        if llm_client is not None and messages is not None:
            follow_up_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                },
                {"role": "tool", "content": json.dumps(output)},
                {"role": "system", "content": _SYNTHESIS_HONESTY_NUDGE},
            ]
            await self._emit({"type": "status", "message": "Writing answer"})
            try:
                # No `tools` offered here on purpose -- this call is meant
                # to produce a final text answer, not choose another tool.
                with self.timer.stage("answer"):
                    follow_up = await generate_with_events(llm_client, follow_up_messages, self.on_event)
            except Exception:
                logger.exception("Follow-up LLM call after tool '%s' failed", name)
            else:
                if follow_up.text:
                    siris.conversation.add_message(conversation_id, role="assistant", content=follow_up.text)
                    return CortexResult(
                        conversation_id=conversation_id, response=follow_up.text, tool_call=tool_call, model_role=model_role
                    )

        return CortexResult(conversation_id=conversation_id, tool_call=tool_call, model_role=model_role)

    async def _private_answer(
        self, siris: "SirisAI", conversation_id: str, name: str, arguments: dict[str, Any], output: Any,
        messages: list[dict[str, Any]] | None, model_role: str,
    ) -> CortexResult:
        """Health data never goes to a cloud model (SIRISAI_HEALTH_PRIVATE): a
        local model writes the answer when one is set up, else SirisAI's own
        summary is the answer. The conversation keeps a placeholder instead of
        the numbers, so later turns sent to the cloud model don't carry them."""
        local = _local_client(siris)
        text = None
        if local is not None and messages is not None:
            follow_up_messages = [
                *messages,
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]},
                {"role": "tool", "content": json.dumps(output, default=str)},
                {"role": "system", "content": _SYNTHESIS_HONESTY_NUDGE},
            ]
            await self._emit({"type": "status", "message": "Writing answer (on this computer)"})
            try:
                text = (await generate_with_events(local, follow_up_messages, self.on_event)).text
            except Exception:
                logger.exception("Local model failed on a private health answer")
        if not text:
            text = output.get("text") if isinstance(output, dict) and output.get("text") else _health_text(siris)
        siris.conversation.add_message(
            conversation_id, role="tool", content=json.dumps({"private": f"{name} result kept on this computer"}), tool_name=name, tool_arguments=arguments
        )
        siris.conversation.add_message(conversation_id, role="assistant", content=f"[Answered from private health data with {name}; kept local.]")
        tool_call = ToolCallOutcome(name=name, arguments=arguments, result=output)
        return CortexResult(conversation_id=conversation_id, response=text, tool_call=tool_call, model_role="local" if local else model_role)

    async def _run_planned(
        self,
        siris: "SirisAI",
        conversation_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        llm_client: Any,
        model_role: str,
    ) -> CortexResult:
        if tools and siris.settings.health_private and not _is_local(siris, llm_client):
            # A plan feeds every result straight back to the model, so health
            # data (SIRISAI_HEALTH_PRIVATE) isn't offered to a cloud one here.
            tools = [t for t in tools if (t.get("function") or t).get("name") not in PRIVATE_TOOLS]
        try:
            plan_result = await Planner(max_steps=self.max_plan_steps, on_event=self.on_event).run(
                siris, conversation_id, messages, tools, llm_client
            )
        except Exception:
            logger.exception("Planner's LLM generate() failed")
            return CortexResult(conversation_id=conversation_id, model_role=model_role, error="LLM backend call failed.")

        for step in plan_result.steps:
            siris.conversation.add_message(
                conversation_id, role="tool", content=json.dumps(step.result), tool_name=step.name, tool_arguments=step.arguments
            )
        plan = [ToolCallOutcome(name=s.name, arguments=s.arguments, result=s.result) for s in plan_result.steps]

        if plan_result.pending_confirmation:
            name, arguments = plan_result.pending_confirmation
            return CortexResult(
                conversation_id=conversation_id, plan=plan, pending_confirmation=(name, arguments), model_role=model_role
            )

        if plan_result.error:
            return CortexResult(
                conversation_id=conversation_id,
                plan=plan,
                model_role=model_role,
                error=plan_result.error,
                permission_denied=plan_result.permission_denied,
            )

        if plan_result.final_text:
            siris.conversation.add_message(conversation_id, role="assistant", content=plan_result.final_text)

        return CortexResult(conversation_id=conversation_id, response=plan_result.final_text, plan=plan, model_role=model_role)

    @staticmethod
    def _tools_run(result: "CortexResult") -> list[str]:
        outcomes = list(result.plan)
        if result.tool_call is not None:
            outcomes.append(result.tool_call)
        return [o.name for o in outcomes]

    def _finalize(self, siris: "SirisAI", result: "CortexResult") -> None:
        """Make sure what the user is told matches what actually happened.

        - No answer text but tools ran: say what ran, rather than letting a
          client show a bare "Done." (found live: a lookup-only turn ended
          "Done." and the light never changed).
        - Answer claims a change ("removed", "turned on", "saved") but every
          tool that ran only read data: append a correction.
        - A tool call is waiting on confirmation: push a notification with a
          one-tap Confirm button (same as a routine's action needing one --
          app/notify/confirmation.py), since the user may not be looking at
          the app right when this happens.
        """
        if result.error:
            return
        if result.pending_confirmation:
            name, arguments = result.pending_confirmation
            try:
                notify_confirmation_needed(siris, "SirisAI", name, arguments)
            except Exception:
                logger.exception("Confirmation-needed notification failed")
            return
        ran = self._tools_run(result)

        def changes_state(tool_name: str) -> bool:
            tool = siris.tools.get(tool_name)
            return tool is not None and tool.permission is not ToolPermission.READ

        if not result.response:
            if ran:
                if any(changes_state(n) for n in ran):
                    result.response = f"I ran {', '.join(ran)} but didn't get a written answer back, so I can't confirm the outcome."
                else:
                    result.response = (
                        f"I looked things up ({', '.join(ran)}) but couldn't put together an answer, "
                        "and I haven't changed anything."
                    )
            return

        if _ACTION_CLAIM_RE.search(result.response) and not any(changes_state(n) for n in ran):
            looked = f" (I only ran: {', '.join(ran)})" if ran else ""
            result.response += f"\n\nNote: I haven't actually changed anything{looked}. Ask me to do it and I'll run the action."
            return

        # Check after acting: a tool's own verify() said the change didn't
        # take (a device that's offline, a container that didn't come back).
        # Say so, rather than leaving "Done" standing.
        failed = self._failed_checks(siris, result.conversation_id, ran)
        if failed:
            result.response += "\n\nHeads up: I checked afterwards and " + "; ".join(failed) + "."

    @staticmethod
    def _failed_checks(siris: "SirisAI", conversation_id: str, ran: list[str]) -> list[str]:
        if not ran:
            return []
        try:
            recent = siris.audit.list_recent(limit=max(10, 3 * len(ran)))
        except Exception:
            return []
        out = []
        for entry in recent:
            if entry["conversation_id"] == conversation_id and entry["tool_name"] in ran and entry.get("verified") is False:
                detail = (entry.get("verification_detail") or f"{entry['tool_name']} didn't take effect").rstrip(".")
                out.append(detail[0].lower() + detail[1:] if detail else detail)
        return out[:3]

    async def _maybe_extract_memory(
        self, siris: "SirisAI", conversation_id: str, context_label: str, result: "CortexResult"
    ) -> None:
        """Automatic memory extraction (see module docstring). `context_label`
        is the user's original prompt for process(), or a short stand-in
        describing what was confirmed for confirm() -- either way, just
        something to show the extraction pass what prompted `result.response`.

        Best-effort and silent: nothing here ever raises out to the caller,
        and it never touches `result` -- a turn's own response reaches the
        user exactly as it would if this didn't exist."""
        if not siris.settings.auto_memory_extraction_enabled:
            return
        taint.begin_turn("", "")
        if not _worth_extracting(context_label):
            return
        if result.error or not result.response or siris.llm is None or siris.model_router is None:
            return

        try:
            exclude = {t.name for t in siris.tools.list()} - _MEMORY_EXTRACTION_TOOL_NAMES
            tools = siris.tools.specs(exclude=exclude)
            if not tools:
                return
            _, llm_client = siris.model_router.pick(siris.llm, context_label, role="fast")
            messages = [
                {"role": "system", "content": _MEMORY_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"User: {context_label}\n\nSirisAI: {result.response}"},
            ]
            await Planner(max_steps=_MEMORY_EXTRACTION_MAX_STEPS).run(
                siris,
                conversation_id,
                messages,
                tools,
                llm_client,
                trigger_template="automatic memory extraction: post-turn review (step {step})",
            )
        except Exception:
            logger.exception("Automatic memory extraction failed")
