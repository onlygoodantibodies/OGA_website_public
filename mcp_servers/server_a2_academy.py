"""Server A2 — the interactive antibody-validation learning connector (PROTOTYPE).

This is the "A2" education track from ``VISION.md`` / ``A2_SCOPING.md``: deliver
the OGA validation curriculum + Champions workshop conversationally, tutor a
scientist through a validation plan for *their own* target, and end by pointing
them at the website to claim an OGA certificate.

**Trust level: LOW / read-only, same as Server A.** This prototype deliberately
does NOT write anything. It:
  * serves the versioned teaching content pack (``mcp_servers/content/``),
  * grades a learner's validation plan against the rubric's mechanical gates,
  * pulls factual antibody/gene evidence through the SAME read-only portal code
    Server A uses (``common/portal.py`` → the ``mcp_readonly`` role in prod), and
  * hands back the website certificate-claim link.

The certificate itself is issued on the website against a verified institutional
email (the public connector can't know identity or write to ``academy_db``), so
this server stays a clean, public, read-only tool. See ``A2_SCOPING.md`` §3.

Stateless by design: the learner's plan lives in the conversation; ``grade_plan``
takes the filled-in plan and returns an objective verdict on the mechanical gates.
"""
from __future__ import annotations

import os
from typing import Optional

from mcp_servers.common import audit, content_pack, links, portal, tutor_guidance

# The website page where a learner claims their certificate. Overridable via env
# for staging. Served by the self-contained `credentials` app (its own DB).
_CLAIM_PATH = os.environ.get("MCP_A2_CLAIM_PATH", "/workshop/claim/")


def build_server(auth_settings=None, token_verifier=None, http_path=None,
                 transport_security=None):
    from mcp.server.fastmcp import FastMCP

    # Boot Django (pipeline_db → the read-only role) so evidence lookups serialise
    # through the canonical public API code, exactly like Server A.
    portal.setup()

    kwargs = {}
    if http_path:
        kwargs["streamable_http_path"] = http_path
    if auth_settings is not None:
        kwargs["auth"] = auth_settings
        kwargs["token_verifier"] = token_verifier
    if transport_security is not None:
        kwargs["transport_security"] = transport_security
    mcp = FastMCP("oga-academy-workshop",
                  instructions=tutor_guidance.SERVER_INSTRUCTIONS, **kwargs)
    register_tools(mcp)
    return mcp


def register_tools(mcp):
    """Register the education / learning tools onto an existing FastMCP instance.

    Assumes Django is already booted (``portal.setup()``). Called both by this
    module's ``build_server`` (the standalone academy server) AND by
    ``server_a_readonly`` — so the SAME education tools are also served on the
    read-only data connector. That lets a single connection surface both the
    antibody data and the interactive education (no second endpoint / OAuth
    resource to register)."""

    @mcp.tool()
    def learning_overview() -> dict:
        """START HERE. Returns the shape of the interactive learning exercise, the
        list of modules, and the non-negotiable learning outcomes. Use it to
        orient yourself, then teach the modules in dialogue (not as walls of
        text), grounding every antibody/gene fact in ``target_evidence`` and never
        inventing performance, prices, or citations."""
        audit.record("A2", "learning_overview")
        return {
            "pack_version": content_pack.pack_version(),
            "overview_markdown": content_pack.get_module("overview")["markdown"],
            "modules": content_pack.list_modules(),
            "journey": [
                "1. Reflect — `choosing_walkthrough`: how do you choose antibodies + "
                "controls now? Unscored.",
                "2. Plan — `get_plan_template` → `grade_plan`: build a validation plan "
                "for a REAL target → earns the Antibody Validation Planning certificate.",
                "3. Framework module — `get_module` → `get_module_quiz` → "
                "`submit_module_quiz` → a module certificate.",
                "4. Controls module — same three tools → a module certificate.",
                "5. Acquiring module — same three tools → a module certificate.",
                "6. Claim — `certificate_claim_link`: claim everything earned on the "
                "website. All 3 modules + the plan also mints the capstone.",
            ],
            "certificates": {
                "module_certificates": {
                    "how": "One per module quiz PASSED via submit_module_quiz "
                           "(server-graded, 70% to pass). This is a REAL gate: always "
                           "grade with the tool and only count a module once it has "
                           "actually passed — never self-assess or wave the learner "
                           "through. Learners may look answers up; that's fine.",
                    "modules": content_pack.certifiable_modules(),
                },
                "planning_certificate": {
                    "name": "Antibody Validation Planning certificate",
                    "how": "Earned by producing a validation plan for the learner's "
                           "OWN target that passes grade_plan (plus the four reasoning "
                           "checks you grade in conversation).",
                    "aka": "internally the 'workshop' certificate — but always use the "
                           "name 'Antibody Validation Planning' with learners.",
                },
                "capstone": {
                    "name": content_pack.capstone_spec().get(
                        "title", "OGA Antibody Validation — Full Certification"),
                    "how": "All three module certificates AND the Antibody Validation "
                           "Planning certificate.",
                    "spec": content_pack.capstone_spec(),
                },
            },
            "signposting": (
                "Keep the journey legible even though it's conversational. At the "
                "start, show the learner the `journey` map above and where they are. "
                "After each module, give a ONE-LINE recap ('✓ Controls done — 1 module "
                "left, then you claim'). Before the claim, summarise exactly what "
                "they've earned and what (if anything) is still needed for the "
                "capstone. Never leave them guessing which step they're on."
            ),
            "pacing": (
                "Make this feel like a colleague at a shared screen, not a chatbot "
                "reading slides. ONE idea per turn (2–4 sentences, ~120 words), then "
                "ask a question and WAIT for the reply. Open each module with a "
                "question, not content. Never paste a module — every teaching module "
                "starts with a tutor-only '🧭 How to teach this' beat plan; follow it "
                "beat by beat. For every figure, have the learner PREDICT a good-vs-"
                "bad result before you reveal it. Let the learner steer the depth."
            ),
            "how_to_finish": (
                "START with `choosing_walkthrough`: ask how the learner currently "
                "chooses an antibody + controls (this is REFLECTION, not scored), "
                "give grounded feedback, then walk them through the decision — "
                "either on a worked example (theoretical) or on a REAL decision "
                "they're facing. The real path produces a validation plan "
                "(get_plan_template → grade_plan) and earns the Antibody Validation "
                "Planning certificate. THEN do the modules: for EACH, teach it "
                "conversationally first (get_module — work through it in dialogue, "
                "use the images, ask questions), and ONLY after that quiz it "
                "(get_module_quiz → submit_module_quiz) to confirm understanding. "
                "Finally call certificate_claim_link with the passed module ids (and "
                "the gene, if they did a real plan) so they claim their certificates "
                "— all modules + a real plan also earns the capstone. A module counts "
                "as passed ONLY when submit_module_quiz returned passed=true — grade "
                "every quiz through the tool (answers stay hidden), and never put a "
                "module in the claim that the learner hasn't actually passed."
            ),
        }

    @mcp.tool()
    def choosing_walkthrough() -> dict:
        """START HERE, before any module or quiz. Returns the tutor guide for the
        opening experience: (1) ask the learner how they CURRENTLY choose an
        antibody + controls for specificity/selectivity — this is REFLECTION, NOT a
        scored test, so don't grade it; (2) reflect their process back with grounded
        feedback (affirm what's good, gently surface gaps), using `target_evidence`
        whenever they name a real antibody/gene; (3) offer to continue either
        THEORETICALLY (a worked example) or on a REAL decision they're facing. The
        real path uses `get_plan_template`/`grade_plan` to shape a validation plan
        and earns the **Antibody Validation Planning** certificate (evidence of
        proper planning), which they can also document on the website's Validation
        Planner/Recorder. After the walkthrough, move on to the modules + quizzes."""
        mod = content_pack.get_module("choosing")
        audit.record("A2", "choosing_walkthrough")
        return {"markdown": mod["markdown"] if mod else "",
                "pack_version": content_pack.pack_version(),
                "modes": ["theoretical", "real"]}

    @mcp.tool()
    def get_module_quiz(module_id: str) -> dict:
        """The short quiz for a module (``framework``, ``controls``, ``acquiring``)
        — questions + options, WITHOUT the answers. **Only call this AFTER you have
        taught the module conversationally (get_module) and the learner has worked
        through the material — never jump straight to the quiz.** The quiz confirms
        understanding at the END of the walkthrough. Present it, then pass their
        chosen option indices to ``submit_module_quiz`` to grade. Passing earns that
        module's certificate."""
        quiz = content_pack.module_quiz(module_id)
        audit.record("A2", "get_module_quiz", module_id=module_id, found=bool(quiz))
        if not quiz:
            return {"found": False, "module_id": module_id,
                    "available": [m["id"] for m in content_pack.certifiable_modules()]}
        note = ("Only present this AFTER teaching the module (get_module) and showing "
                "its figures — do not shortcut a module to its quiz. This quiz is the "
                "REAL gate for the module certificate: collect the learner's own "
                "answers, grade them with submit_module_quiz (server-graded), and "
                "don't reveal the correct options or answer on their behalf. A learner "
                "looking answers up is fine; a rubber-stamped pass is not.")
        return {"found": True, "tutor_note": note, **quiz}

    @mcp.tool()
    def submit_module_quiz(module_id: str, answers: list) -> dict:
        """Grade a module quiz — this result is AUTHORITATIVE; report it exactly and
        never grade in your head or invent gaps. ``answers`` is the learner's chosen
        option for each question, in order, as letters (``a``/``b``/``c``/``d``) or
        0-based indices — pass exactly what they chose. Returns ``passed``, the score,
        and per-question correctness + explanations — teach the misses, and the learner
        may retry. When ``passed`` is true, tell them they can claim this module's
        certificate (collect the passed module ids and hand them to
        ``certificate_claim_link``)."""
        result = content_pack.grade_module_quiz(module_id, answers)
        audit.record("A2", "submit_module_quiz", module_id=module_id,
                     passed=result.get("passed"))
        return result

    @mcp.tool()
    def get_module(module_id: str) -> dict:
        """Return one teaching module's content as markdown. ``module_id`` is one
        of the ids from ``learning_overview`` (``framework``, ``controls``,
        ``acquiring``, ``overview``). **Teach it as a conversation, not a lecture** —
        work through the ideas in your own words, ask the learner questions, invite
        theirs, and check they follow before moving on (especially for the controls
        module, where the misconception — reaching for an isotype/peptide-block as a
        'specificity' control — is the teachable moment).

        **Pace it — do NOT paste the module.** Each module opens with a tutor-only
        '🧭 How to teach this' beat plan: follow it **beat by beat**. Deliver ONE
        idea (2–4 sentences, ~120 words), ask a question, and WAIT for the learner's
        reply before the next. Open with a question, not content. For every figure,
        ask the learner to predict a good-vs-bad result BEFORE revealing it. Do this
        walkthrough FIRST; the quiz (get_module_quiz) comes only after the learner
        has engaged with the material."""
        mod = content_pack.get_module(module_id)
        audit.record("A2", "get_module", module_id=module_id, found=bool(mod))
        if not mod:
            return {"found": False, "module_id": module_id,
                    "available": [m["id"] for m in content_pack.list_modules()]}
        return {"found": True, **mod}

    @mcp.tool()
    def classify_control(kind: str) -> dict:
        """Does a control TYPE establish antibody selectivity? Pass a control kind
        (e.g. ``knockout``, ``overexpression_lysate``, ``isotype``,
        ``peptide_block``, ``secondary_only``, ``loading_control``). Returns
        whether it establishes selectivity or answers a different question. This
        encodes the sharpest learning outcome — use it to check a learner's
        proposed control before confirming it."""
        audit.record("A2", "classify_control", kind=kind)
        return content_pack.classify_control(kind)

    @mcp.tool()
    def get_plan_template() -> dict:
        """Return the validation-plan JSON schema + an empty plan skeleton for the
        learner to fill in for THEIR OWN target. The plan is the framework's
        7-item checklist; it's the assessment artifact and the evidence behind the
        Antibody Validation Planning certificate. Fill it collaboratively across the
        conversation, then submit it to ``grade_plan``."""
        audit.record("A2", "get_plan_template")
        schema = content_pack.plan_schema()
        skeleton = {
            "target_gene": "", "application": "", "sample_type": "",
            "question_type": "", "question_statement": "",
            "evidence_search": {"sources_checked": [],
                                "found_genetic_validation": False, "notes": ""},
            "controls": {"positive": [], "negative": [], "how_matched": ""},
            "antibody_identity": {}, "orthogonal_readouts": [],
            "decision": "", "decision_rationale": "",
        }
        return {"schema": schema, "skeleton": skeleton,
                "pack_version": content_pack.pack_version()}

    @mcp.tool()
    def target_evidence(gene: str) -> dict:
        """Factual OGA evidence for a gene — the same read-only, portal-parity data
        Server A returns (published antibodies with per-application assessment +
        KO-controlled evidence, report DOIs). Use it while planning the learner's
        controls/evidence so guidance is grounded, never invented. Absence from the
        dataset is NOT a verdict on an antibody."""
        result = portal.gene_detail(gene)
        audit.record("A2", "target_evidence", gene=gene,
                     found=result.get("found"))
        return result

    @mcp.tool()
    def grade_plan(plan: dict) -> dict:
        """Grade a learner's validation plan against the rubric's MECHANICAL hard
        gates (e.g. 'has a genuine positive AND negative selectivity control',
        'not leaning only on peptide-block/isotype/secondary/loading'). Returns a
        pass/fail per gate plus the open-ended reasoning checks YOU must still
        grade in conversation. ``passed_mechanical`` is a necessary floor, not
        sufficient on its own — a certificate needs the reasoning checks too."""
        result = content_pack.grade_plan(plan or {})
        audit.record("A2", "grade_plan",
                     passed_mechanical=result["passed_mechanical"])
        return result

    @mcp.tool()
    def certificate_claim_link(completed_modules: Optional[list] = None,
                               gene: Optional[str] = None) -> dict:
        """How the learner claims their OGA certificate(s). Pass
        ``completed_modules`` — the ids of the modules whose quiz they PASSED
        (``framework``/``controls``/``acquiring``) — and optionally ``gene`` (the
        target of the real plan they built); the returned URL is pre-filled. The
        public connector does NOT issue certificates — issuance happens on the
        website against a verified institutional email (one email confirmation issues
        every earned certificate; all three modules + the Antibody Validation
        Planning plan also mints the capstone / full certification).

        Also returns a ``status`` (earned so far / still needed for the capstone) —
        read it back to the learner so they always know where they stand.

        Only pass modules the learner actually passed via ``submit_module_quiz``,
        and only pass ``gene`` if their plan passed ``grade_plan``."""
        from urllib.parse import urlencode
        all_ids = [m["id"] for m in content_pack.certifiable_modules()]
        valid = set(all_ids)
        mods = [m for m in (completed_modules or []) if m in valid]
        audit.record("A2", "certificate_claim_link", modules=mods)
        base = links.base_url()
        query = {}
        if mods:
            query["modules"] = ",".join(mods)
        if gene:
            query["gene"] = gene
        url = base + _CLAIM_PATH + (f"?{urlencode(query)}" if query else "")

        # A plain-language status so the learner never loses the thread of what
        # they've earned and what's left for the capstone.
        plan_done = bool(gene)
        missing_modules = [m for m in all_ids if m not in mods]
        earned = [f"{m} module certificate" for m in mods]
        if plan_done:
            earned.append("Antibody Validation Planning certificate")
        still_needed = [f"pass the {m} module quiz (submit_module_quiz)"
                        for m in missing_modules]
        if not plan_done:
            still_needed.append("produce a validation plan for a real target that "
                                "passes grade_plan (earns the Antibody Validation "
                                "Planning certificate)")
        capstone_ready = not missing_modules and plan_done

        return {
            "claim_url": url,
            "status": {
                "earned_so_far": earned or ["nothing yet"],
                "capstone_ready": capstone_ready,
                "still_needed_for_capstone": [] if capstone_ready else still_needed,
                "summary": ("All requirements met — claiming will also mint the "
                            "capstone (full certification)." if capstone_ready else
                            "Not the full set yet. Still needed for the capstone: "
                            + "; ".join(still_needed) + "."),
            },
            "requires": [
                "an institutional email (verified on the site — free providers "
                "like gmail/outlook are not accepted)",
                "your name, as it should appear on the certificate",
                "for the Antibody Validation Planning certificate: the validation "
                "plan you produced for your real target (paste it on the claim page)",
            ],
            "note": ("One email confirmation issues every certificate you've "
                     "earned. Each is recorded in the OGA database and downloadable "
                     "as a verifiable PDF (QR + code). The Antibody Validation "
                     "Planning certificate is evidence of proper validation "
                     "planning for a real target — document it formally with the "
                     "Validation Planner/Recorder at "
                     f"{base}/tools/validation-planner/ . In-person workshop "
                     "attendees can also request a certificate the OGA team issues "
                     "manually."),
        }

    return mcp


def main():
    build_server().run()


if __name__ == "__main__":
    main()
