"""The question asked before the scan, and what its answer is allowed to do.

WHY ASK AT ALL
--------------
Two jobs, and the smaller one is the obvious one.

**Priming.** Knowledge a model articulates itself is more active than knowledge
handed to it in a rubric it skimmed. A reader who has just written "you have to
remove the target and show the signal goes" reads a Methods section differently
from one that was told the same sentence by a server. That is the whole reason
this is an open question and not a checkbox.

**Sizing.** The answer says how much support this reader needs, which is how you
would do it with a person: find out what they already know, then supply the gap
and nothing more. The alternative — inferring competence from the payload — can
only judge a payload that has already been assembled, by which point the paper
has been read and the support arrives too late to change how.

THE ONE PROPERTY THAT MAKES IT SAFE
-----------------------------------
**The answer may only ever ADD support, never withhold it.** A good answer earns
a shorter reply; a poor one, an unrecognisable one, or none at all gets the full
scaffold. Nothing here can take anything away from a caller.

That asymmetry is what lets this file get away with matching strings, which is
otherwise the exact layer this server tore out. Being wrong costs tokens in one
direction and nothing in the other, so a crude test is a fair test. It is also
what stops the question becoming an exam: there is no score to game, no penalty
to avoid, and the answer is echoed to the reader unjudged.

WHAT IT MUST NEVER TOUCH
------------------------
Classification of the paper's controls. This is the model's view of controls in
general; letting it reach a verdict about a specific paper's controls would be
opinion leaking into the fact layer, which is the one thing this server exists
to keep from happening.

WHY THE QUESTION IS OPEN AND THE ANSWER IS ECHOED
--------------------------------------------------
A leading question anchors: ask "what shows selectivity?" and you invite the
answer you already believe, and suppress a reader who would have raised
something worth hearing — epitope availability differing between a section and a
coverslip, a case where orthogonal evidence outweighs a knockdown. So the
question stays open, the answer is echoed verbatim and unscored, and if readers
keep raising something the rubric does not cover, that is a finding about the
rubric.
"""
from __future__ import annotations

import re as _re

#: Asked on the tool itself, so it is answered BEFORE the paper is read rather
#: than after the payload is assembled.
QUESTION = (
    "In your own words, what would show that an antibody is selectively "
    "detecting its target — and what would not? Answer from your own knowledge, "
    "before and independently of anything this server tells you."
)

#: A control that removes the TARGET, in the words a reader would reach for. The
#: list is short on purpose: it decides only whether to send more text, so a term
#: missing from it costs a few hundred tokens and never a verdict.
_GENETIC = _re.compile(
    r"\b(knock\s?-?\s?outs?|knock\s?-?\s?downs?|kos?|crispr|cas9|sirna|shrna|"
    r"rnai|morpholino|null|deleti\w+|depleti\w+|silenc\w+|ablat\w+|"
    r"genetic\w*)\b", _re.I)

#: The classic mistakes. Naming one is not a failure — a good answer names them
#: precisely to rule them out — so this is never read on its own.
_PSEUDO = _re.compile(
    r"\b(peptide|antigen\s+competition|pre\s?-?\s?ab?sorption|blocking\s+peptide|"
    r"isotype|no\s+primary|secondary[-\s]only|omit\w*\s+the\s+primary)\b", _re.I)

NOT_ANSWERED_NOTE = (
    "You were asked what would show an antibody is selectively detecting its "
    "target, and did not answer. That question is not a formality and it is not "
    "scored — it is asked because a reader who has just stated the principle "
    "applies it better than one who was handed it, and because the answer is how "
    "this reply decides how much to explain. Unanswered, it explains everything."
)


def assess(answer):
    """``(covered, why)`` — may this reply be shorter?

    ``covered`` is True only when the answer names a control that removes the
    TARGET. Nothing else is asked of it. A bare mention counts, deliberately: the
    bar is low because the consequence is low, and a higher bar would be a mark
    out of ten by another name.

    An answer that names only pseudo-controls is the case worth catching. It is
    the commonest wrong model of what a specificity control is, and the reader
    holding it is exactly the one the scaffold was written for.
    """
    text = (answer or "").strip()
    if not text:
        return False, "not answered"
    if _GENETIC.search(text):
        return True, "named a control that removes the target"
    if _PSEUDO.search(text):
        return False, ("named only controls that remove or occupy the ANTIBODY, "
                       "not the target")
    return False, "did not name a control that removes the target"
