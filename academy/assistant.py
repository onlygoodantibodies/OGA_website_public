"""OGA in-app AI assistant — the hosted chat behind the academy login.

Two modes, sharing one engine:

* ``database``  — factual antibody/gene lookups over OGA's published data.
* ``elearning`` — the conversational validation-training tutor.

Both call the **same underlying functions the read-only MCP server uses**
(``mcp_servers.common.portal`` for data, ``mcp_servers.common.content_pack`` for
the teaching pack), so the hosted chat and the "connect your own AI" MCP endpoint
never drift apart. Nothing here writes to the database — the data tools are
read-only, and certificate issuance stays on the existing ``credentials`` flow
(the tutor just hands back the pre-filled claim link, exactly like the MCP tool).

Claude is called over raw HTTPS with ``requests`` (mirroring
``pipeline/services/antibody_search.py`` — the app's existing Claude call), so no
new dependency is introduced. The model, per-message cap, and tool-round cap are
the cost guardrails; the system prompt scopes the assistant to OGA topics.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests
from django.conf import settings

from mcp_servers.common import tutor_guidance

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# --- cost guardrails (overridable via env-backed settings) ------------------
MODEL = getattr(settings, "ACADEMY_AI_MODEL", "claude-haiku-4-5")
MAX_TOKENS = int(getattr(settings, "ACADEMY_AI_MAX_TOKENS", 1500))
MAX_TOOL_ROUNDS = int(getattr(settings, "ACADEMY_AI_MAX_TOOL_ROUNDS", 6))
TIMEOUT_SECONDS = int(getattr(settings, "ACADEMY_AI_TIMEOUT", 60))
# Most a single POST may carry back to us (defends against pasted walls of text).
MAX_HISTORY_MESSAGES = 40
MAX_MESSAGE_CHARS = 6000

MODES = ("elearning", "database")


def is_configured() -> bool:
    """True when the Anthropic API key is set (mirrors antibody_search)."""
    return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))


def tutor_offline() -> bool:
    """True when the conversational e-learning tutor is taken offline for its
    rework. Only affects the 'elearning' mode — database search is unaffected."""
    return bool(getattr(settings, "AI_TUTOR_OFFLINE", False))


def offline_message() -> str:
    return getattr(settings, "AI_TUTOR_OFFLINE_MESSAGE",
                   "Our AI tutor is temporarily offline for improvements.")


# ---------------------------------------------------------------------------
# In-process tool implementations — thin wrappers over the SAME functions the
# MCP server exposes. No portal.setup(): Django is already booted in the web
# process, and these functions only ever read.
# ---------------------------------------------------------------------------

def _portal():
    from mcp_servers.common import portal
    return portal


def _content():
    from mcp_servers.common import content_pack
    return content_pack


def _links():
    from mcp_servers.common import links
    return links


# Heavy, chat-irrelevant keys in the portal serialisation — the only things a
# TEXT chat doesn't need and that dominate token cost (per-figure image URLs and
# the 5-way embed links). Everything else is passed through VERBATIM, so the chat
# sees the exact same fields/keys the MCP server returns (no re-mapping, so a field
# can never be silently renamed or dropped — the class of bug that hid product URLs).
_ANTIBODY_DROP_KEYS = ("experiments", "embed_urls")


def _slim_antibody(d: dict) -> dict:
    """Return the portal's antibody record unchanged except for the bulky media
    keys. Identical shape to the read-only MCP server's output (incl.
    ``metadata.product_link``), just lighter."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in _ANTIBODY_DROP_KEYS}


# --- data tools (database mode) --------------------------------------------

def _t_list_targets(only_with_recommendations: bool = False) -> dict:
    rows = _portal().list_targets(only_with_recommendations=bool(only_with_recommendations))
    return {"count": len(rows), "targets": rows}


def _t_search_antibodies(text: str, limit: int = 25) -> dict:
    rows, truncated = _portal().search_antibodies(text, limit=min(int(limit or 25), 50))
    return {"count": len(rows), "truncated": truncated,
            "antibodies": [_slim_antibody(r) for r in rows]}


def _t_target_report(gene: str) -> dict:
    d = _portal().gene_detail(gene)
    if not d.get("found"):
        return d
    d["antibodies"] = [_slim_antibody(a) for a in d.get("antibodies", [])]
    return d


def _t_antibodies_by_recommendation(gene: str, application: str,
                                    recommended: bool = True) -> dict:
    rows, truncated = _portal().antibodies_by_recommendation(
        application, recommended=bool(recommended), gene=gene)
    return {"count": len(rows), "truncated": truncated,
            "antibodies": [_slim_antibody(r) for r in rows]}


def _t_antibody_validation(rrid: Optional[str] = None,
                           catalogue: Optional[str] = None,
                           gene: Optional[str] = None) -> dict:
    if not (rrid or catalogue or gene):
        return {"error": "Provide at least one of: rrid, catalogue, gene."}
    rows, truncated = _portal().antibody_validation(rrid=rrid, catalogue=catalogue, gene=gene)
    if not rows:
        return {"in_dataset": False,
                "note": "Not in the OGA dataset. Absence is NOT a verdict on quality."}
    return {"in_dataset": True, "truncated": truncated,
            "matches": [_slim_antibody(r) for r in rows]}


# --- education tools (elearning mode) --------------------------------------

def _t_learning_overview() -> dict:
    cp = _content()
    return {
        "pack_version": cp.pack_version(),
        "modules": cp.list_modules(),
        "certifiable_modules": cp.certifiable_modules(),
        "capstone": cp.capstone_spec(),
        "journey": [
            "1. choosing_walkthrough — reflect on how you choose antibodies + controls (unscored).",
            "2. get_plan_template -> grade_plan — build a validation plan for a REAL target (Antibody Validation Planning certificate).",
            "3-5. For each module (framework, controls, acquiring): get_module -> get_module_quiz -> submit_module_quiz (a module certificate).",
            "6. certificate_claim_link — claim everything earned. All 3 modules + the plan mints the capstone.",
        ],
    }


def _t_choosing_walkthrough() -> dict:
    mod = _content().get_module("choosing")
    return {"markdown": mod["markdown"] if mod else "", "modes": ["theoretical", "real"]}


def _t_get_module(module_id: str) -> dict:
    mod = _content().get_module(module_id)
    if not mod:
        return {"found": False, "module_id": module_id,
                "available": [m["id"] for m in _content().list_modules()]}
    return {"found": True, **mod}


def _t_get_module_quiz(module_id: str) -> dict:
    quiz = _content().module_quiz(module_id)
    if not quiz:
        return {"found": False, "module_id": module_id,
                "available": [m["id"] for m in _content().certifiable_modules()]}
    return {"found": True, **quiz}


def _t_submit_module_quiz(module_id: str, answers: list) -> dict:
    return _content().grade_module_quiz(module_id, answers or [])


def _t_classify_control(kind: str) -> dict:
    return _content().classify_control(kind)


def _t_get_plan_template() -> dict:
    cp = _content()
    skeleton = {
        "target_gene": "", "application": "", "sample_type": "",
        "question_type": "", "question_statement": "",
        "evidence_search": {"sources_checked": [], "found_genetic_validation": False, "notes": ""},
        "controls": {"positive": [], "negative": [], "how_matched": ""},
        "antibody_identity": {}, "orthogonal_readouts": [],
        "decision": "", "decision_rationale": "",
    }
    return {"schema": cp.plan_schema(), "skeleton": skeleton, "pack_version": cp.pack_version()}


def _t_grade_plan(plan: dict) -> dict:
    return _content().grade_plan(plan or {})


def _t_target_evidence(gene: str) -> dict:
    d = _portal().gene_detail(gene)
    if not d.get("found"):
        return d
    d["antibodies"] = [_slim_antibody(a) for a in d.get("antibodies", [])]
    return d


def _t_certificate_claim_link(completed_modules: Optional[list] = None,
                              gene: Optional[str] = None) -> dict:
    from urllib.parse import urlencode
    cp = _content()
    all_ids = [m["id"] for m in cp.certifiable_modules()]
    valid = set(all_ids)
    mods = [m for m in (completed_modules or []) if m in valid]
    base = _links().base_url()
    query = {}
    if mods:
        query["modules"] = ",".join(mods)
    if gene:
        query["gene"] = gene
    url = base + "/workshop/claim/" + (f"?{urlencode(query)}" if query else "")
    plan_done = bool(gene)
    missing = [m for m in all_ids if m not in mods]
    earned = [f"{m} module certificate" for m in mods]
    if plan_done:
        earned.append("Antibody Validation Planning certificate")
    still_needed = [f"pass the {m} module quiz" for m in missing]
    if not plan_done:
        still_needed.append("produce a validation plan for a real target that passes grade_plan")
    capstone_ready = not missing and plan_done
    return {
        "claim_url": url,
        "earned_so_far": earned or ["nothing yet"],
        "capstone_ready": capstone_ready,
        "still_needed_for_capstone": [] if capstone_ready else still_needed,
        "requires": [
            "an institutional email (verified on the site — free providers like gmail/outlook are not accepted)",
            "your name, as it should appear on the certificate",
            "for the Antibody Validation Planning certificate: the validation plan you produced (paste it on the claim page)",
        ],
        "note": ("One email confirmation issues every certificate you've earned. Each is "
                 "recorded in the OGA database and downloadable as a verifiable PDF (QR + code)."),
    }


# ---------------------------------------------------------------------------
# Tool registries + JSON schemas per mode. Descriptions are lifted from the MCP
# server tool docstrings so the hosted chat behaves identically to the connector.
# ---------------------------------------------------------------------------

_DATA_TOOLS = [
    {
        "name": "search_antibodies",
        "description": ("Locate a specific published antibody in the OGA dataset by catalogue "
                        "number, RRID, or gene — the 'is this antibody in the dataset?' lookup. "
                        "Does NOT search by company."),
        "input_schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "catalogue number, RRID, or gene symbol"},
            "limit": {"type": "integer", "description": "max rows (default 25, max 50)"}},
            "required": ["text"]},
        "fn": _t_search_antibodies,
    },
    {
        "name": "list_targets",
        "description": ("List the public genes (targets) OGA has characterised. Returns gene + "
                        "protein/UniProt identity only. only_with_recommendations keeps genes "
                        "with at least one recommended antibody."),
        "input_schema": {"type": "object", "properties": {
            "only_with_recommendations": {"type": "boolean"}}, "required": []},
        "fn": _t_list_targets,
    },
    {
        "name": "target_report",
        "description": ("Full public report for ONE gene: the target, its published antibodies "
                        "with per-application assessment + KO-controlled evidence, a supplier "
                        "summary, and report DOIs. Use this to explain, factually, what OGA "
                        "recommends for a gene and why."),
        "input_schema": {"type": "object", "properties": {
            "gene": {"type": "string"}}, "required": ["gene"]},
        "fn": _t_target_report,
    },
    {
        "name": "antibodies_by_recommendation",
        "description": ("Within ONE gene, the antibodies (not) recommended for an application. "
                        "gene is REQUIRED. application is one of WB, IP, IF, FC. Set "
                        "recommended=false for the tested-but-not-recommended ones (with the "
                        "supporting KO-controlled evidence)."),
        "input_schema": {"type": "object", "properties": {
            "gene": {"type": "string"},
            "application": {"type": "string", "enum": ["WB", "IP", "IF", "FC"]},
            "recommended": {"type": "boolean"}}, "required": ["gene", "application"]},
        "fn": _t_antibodies_by_recommendation,
    },
    {
        "name": "antibody_validation",
        "description": ("Look up ONE antibody and report, factually, how it performed when OGA "
                        "tested it independently with knockout controls. Accepts an RRID, "
                        "catalogue number, or gene. If not present, returns in_dataset=false — "
                        "state that as a fact; absence is NOT evidence about quality."),
        "input_schema": {"type": "object", "properties": {
            "rrid": {"type": "string"}, "catalogue": {"type": "string"},
            "gene": {"type": "string"}}, "required": []},
        "fn": _t_antibody_validation,
    },
]

_EDU_TOOLS = [
    {"name": "learning_overview",
     "description": ("START HERE. Returns the shape of the learning exercise, the modules, and "
                     "the certificate journey. Orient yourself, then teach in dialogue — never "
                     "walls of text — grounding every antibody/gene fact in target_evidence."),
     "input_schema": {"type": "object", "properties": {}, "required": []},
     "fn": _t_learning_overview},
    {"name": "choosing_walkthrough",
     "description": ("START HERE before any module. Tutor guide for the opening: ask how the "
                     "learner CURRENTLY chooses an antibody + controls (reflection, NOT scored), "
                     "reflect it back with grounded feedback, then offer a theoretical worked "
                     "example or a REAL decision they face (which builds a validation plan)."),
     "input_schema": {"type": "object", "properties": {}, "required": []},
     "fn": _t_choosing_walkthrough},
    {"name": "get_module",
     "description": ("Return one teaching module's markdown (framework, controls, acquiring, "
                     "overview). Teach it as a conversation, not a lecture: follow the module's "
                     "'How to teach this' beat plan, ONE idea per turn (~120 words), ask a "
                     "question and WAIT. Do this BEFORE the quiz."),
     "input_schema": {"type": "object", "properties": {
         "module_id": {"type": "string"}}, "required": ["module_id"]},
     "fn": _t_get_module},
    {"name": "get_module_quiz",
     "description": ("The short quiz for a module (framework, controls, acquiring), WITHOUT the "
                     "answers. Only call AFTER teaching the module conversationally. Present it, "
                     "collect the learner's chosen option indices, then submit_module_quiz."),
     "input_schema": {"type": "object", "properties": {
         "module_id": {"type": "string"}}, "required": ["module_id"]},
     "fn": _t_get_module_quiz},
    {"name": "submit_module_quiz",
     "description": ("Grade a module quiz — this result is AUTHORITATIVE; report it exactly and "
                     "NEVER grade in your head or invent gaps. answers is the learner's chosen "
                     "option for each question, in order, as letters (a/b/c/d) — pass exactly "
                     "what they chose. Returns passed, score, and per-question correctness + "
                     "explanations. Passing earns that module's certificate; the learner may "
                     "retry. The REAL gate — never rubber-stamp, never overrule the tool."),
     "input_schema": {"type": "object", "properties": {
         "module_id": {"type": "string"},
         "answers": {"type": "array", "items": {"type": ["string", "integer"]}}},
         "required": ["module_id", "answers"]},
     "fn": _t_submit_module_quiz},
    {"name": "classify_control",
     "description": ("Does a control TYPE establish antibody selectivity? Pass a kind (knockout, "
                     "overexpression_lysate, isotype, peptide_block, secondary_only, "
                     "loading_control). Use it to check a learner's proposed control."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string"}}, "required": ["kind"]},
     "fn": _t_classify_control},
    {"name": "get_plan_template",
     "description": ("Return the validation-plan JSON schema + an empty skeleton for the learner "
                     "to fill for THEIR OWN target. Fill it collaboratively, then grade_plan."),
     "input_schema": {"type": "object", "properties": {}, "required": []},
     "fn": _t_get_plan_template},
    {"name": "grade_plan",
     "description": ("Grade a learner's validation plan against the rubric's MECHANICAL hard "
                     "gates. Returns pass/fail per gate plus the open-ended reasoning checks YOU "
                     "grade in conversation. passed_mechanical is a necessary floor, not "
                     "sufficient alone."),
     "input_schema": {"type": "object", "properties": {
         "plan": {"type": "object"}}, "required": ["plan"]},
     "fn": _t_grade_plan},
    {"name": "target_evidence",
     "description": ("Factual OGA evidence for a gene (published antibodies + per-application "
                     "assessment + KO-controlled evidence). Use it while planning the learner's "
                     "controls/evidence so guidance is grounded, never invented."),
     "input_schema": {"type": "object", "properties": {
         "gene": {"type": "string"}}, "required": ["gene"]},
     "fn": _t_target_evidence},
    {"name": "certificate_claim_link",
     "description": ("How the learner claims their OGA certificate(s). Pass completed_modules "
                     "(ids of modules whose quiz they PASSED) and optionally gene (their real "
                     "plan's target). Issuance happens on the website against a VERIFIED "
                     "INSTITUTIONAL EMAIL. Only pass modules actually passed via "
                     "submit_module_quiz, and gene only if the plan passed grade_plan."),
     "input_schema": {"type": "object", "properties": {
         "completed_modules": {"type": "array", "items": {"type": "string"}},
         "gene": {"type": "string"}}, "required": []},
     "fn": _t_certificate_claim_link},
]

_TOOLS = {"database": _DATA_TOOLS, "elearning": _EDU_TOOLS}


def _api_tools(mode: str) -> list:
    """Tool defs in Anthropic wire format (strip our local ``fn``)."""
    return [{k: v for k, v in t.items() if k != "fn"} for t in _TOOLS[mode]]


def _dispatch(mode: str, name: str, tool_input: dict) -> dict:
    for t in _TOOLS[mode]:
        if t["name"] == name:
            try:
                return t["fn"](**(tool_input or {}))
            except TypeError as e:
                return {"error": f"bad arguments for {name}: {e}"}
            except Exception as e:  # never let a tool crash the whole turn
                logger.exception("assistant tool %s failed", name)
                return {"error": f"{name} failed: {e}"}
    return {"error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_COMMON_GUARDRAIL = (
    "You are the Only Good Antibodies (OGA) assistant, embedded on the OGA website "
    "behind the academy login. You ONLY help with antibody validation, antibody/gene "
    "selection, OGA's published antibody data, and OGA's validation training. If asked "
    "about anything unrelated (general chit-chat, coding, other topics), briefly and "
    "politely decline and steer back to antibodies/validation. Never invent antibody "
    "performance, prices, catalogue numbers, RRIDs, citations, or recommendations — if a "
    "fact isn't in a tool result, say you don't have it. Keep replies focused and "
    "reasonably concise."
)

# The tutoring/data behaviour lives in mcp_servers.common.tutor_guidance so it is
# shared with the MCP server's `instructions` (the BYO path) and can't drift.
_SYSTEM = {
    "database": _COMMON_GUARDRAIL + " MODE: antibody database search. " + tutor_guidance.DATA_STYLE,
    "elearning": _COMMON_GUARDRAIL + " " + tutor_guidance.TUTOR_STYLE,
}


# ---------------------------------------------------------------------------
# The chat turn
# ---------------------------------------------------------------------------

def _sanitise_history(messages: list) -> list:
    """Coerce client-sent history into clean {role, content(str)} turns."""
    out = []
    for m in (messages or [])[-MAX_HISTORY_MESSAGES:]:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()[:MAX_MESSAGE_CHARS]
        if content:
            out.append({"role": role, "content": content})
    # API requires the conversation to start with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _anthropic_call(system: str, tools: list, messages: list) -> dict:
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    # Cache the stable prefix (tools + system) so it's billed at ~0.1x on repeat
    # calls. A cache_control breakpoint on the system block covers the tools too
    # (render order is tools -> system -> messages). NOTE: this only takes effect
    # on models whose minimum cacheable prefix is small enough — on Haiku 4.5 the
    # ~1.5-2.4k-token prefix is under the 4096 floor, so it's a silent no-op there
    # and starts saving automatically on Sonnet. Quality/latency are unaffected.
    resp = requests.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS,
              "system": [{"type": "text", "text": system,
                          "cache_control": {"type": "ephemeral"}}],
              "tools": tools, "messages": messages},
        timeout=TIMEOUT_SECONDS,
    )
    return resp


def run_turn(mode: str, messages: list) -> dict:
    """Run one assistant turn (with an internal tool-use loop).

    Returns ``{"ok": bool, "reply": str, "error": str|None, "usage": {...}}``.
    """
    if mode not in MODES:
        return {"ok": False, "error": "invalid mode", "reply": ""}
    if mode == "elearning" and tutor_offline():
        return {"ok": False, "error": "tutor_offline", "reply": offline_message()}
    if not is_configured():
        return {"ok": False, "error": "not_configured",
                "reply": "The AI assistant isn't configured on this server yet."}

    history = _sanitise_history(messages)
    if not history:
        return {"ok": False, "error": "empty", "reply": "Please type a message."}

    system = _SYSTEM[mode]
    tools = _api_tools(mode)
    convo = [dict(m) for m in history]
    in_tokens = out_tokens = cache_read = cache_write = 0

    def _usage():
        return {"input_tokens": in_tokens, "output_tokens": out_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write}

    try:
        for _round in range(MAX_TOOL_ROUNDS):
            resp = _anthropic_call(system, tools, convo)
            if resp.status_code != 200:
                logger.error("assistant API error %s: %s", resp.status_code, resp.text[:400])
                return {"ok": False, "error": f"api_error_{resp.status_code}",
                        "reply": "Sorry — the assistant hit an error. Please try again."}
            data = resp.json()
            usage = data.get("usage", {}) or {}
            in_tokens += usage.get("input_tokens", 0)
            out_tokens += usage.get("output_tokens", 0)
            cache_read += usage.get("cache_read_input_tokens", 0) or 0
            cache_write += usage.get("cache_creation_input_tokens", 0) or 0
            content = data.get("content", []) or []
            convo.append({"role": "assistant", "content": content})

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if data.get("stop_reason") != "tool_use" or not tool_uses:
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                return {"ok": True, "reply": text or "…", "usage": _usage()}

            results = []
            for tu in tool_uses:
                out = _dispatch(mode, tu.get("name"), tu.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": tu.get("id"),
                                "content": json.dumps(out, default=str)})
            convo.append({"role": "user", "content": results})

        # Ran out of tool rounds — return whatever text we have.
        return {"ok": True,
                "reply": ("I've gathered a lot from the tools — could you narrow the "
                          "question a little so I can give you a clear answer?"),
                "usage": _usage()}
    except requests.Timeout:
        return {"ok": False, "error": "timeout", "reply": "That took too long — please try again."}
    except requests.RequestException as e:
        logger.error("assistant request failed: %s", e)
        return {"ok": False, "error": "network", "reply": "Network error — please try again."}
    except Exception as e:  # noqa: BLE001 - defensive; never 500 the chat endpoint
        logger.exception("assistant turn crashed")
        return {"ok": False, "error": "crash", "reply": "Something went wrong — please try again."}
