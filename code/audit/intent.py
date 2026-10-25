"""Deterministic request-intent classification.

A request is NON_DELETION only when it carries an explicit non-deletion signal
(a negated deletion, a keep-the-account instruction, or a scope limited with
"only") and no affirmative deletion directive. Anything we cannot classify
confidently is AMBIGUOUS and is audited as a deletion request, so an unusual
deletion request is never silently dropped as NOT_APPLICABLE.
"""
import re

DELETION = "DELETION"
NON_DELETION = "NON_DELETION"
AMBIGUOUS = "AMBIGUOUS"

# Deletion vocabulary: en, pt, es, fr, ja, ko.
_DELETE = re.compile(
    r"delet|eras|remov|\bclos(e|ing|ure)\b|"
    r"exclus|exclu[ií]|apag|elimin|borr|suprim|suppr|effac|"
    r"削除|消去|退会|삭제|탈퇴",
    re.I,
)
# Plain "no" is excluded on purpose: "I no longer use this account ... delete it" is a deletion.
_NEGATION = re.compile(
    r"\bnot\b|n't\b|\bwithout\b|\bnever\b|\bnão\b|\bno (quiero|deseo|elimin|borr)|\bpas\b|\bsans\b|しないで|않|말아",
    re.I,
)
_LIMITED = re.compile(r"\bonly\b|\bapenas\b|\bsolo\b|\bseulement\b", re.I)
_KEEP = re.compile(
    r"\b(keep|leave)\b[^.]*\b(account|profile|records?|history)\b|"
    r"\bcontinue (using|to use)\b|\bstill (need|want|use)\b",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?。？！])\s*")


def classify(text):
    deletion = non_deletion = False
    for sentence in filter(None, (s.strip() for s in _SENTENCE.split(text or ""))):
        if _KEEP.search(sentence):
            non_deletion = True
        if _DELETE.search(sentence):
            if _NEGATION.search(sentence) or _LIMITED.search(sentence):
                non_deletion = True
            else:
                deletion = True
    if deletion and not non_deletion:
        return DELETION
    if non_deletion and not deletion:
        return NON_DELETION
    return AMBIGUOUS


def decide(rule, llm):
    """Combine the rule label with Claude's label (None when not cached).

    Agreement keeps the label; an AMBIGUOUS rule label takes Claude's reading; any other
    disagreement is audited as a deletion, so neither reader alone can drop a deletion request.
    """
    if llm is None or llm == rule:
        return rule
    return llm if rule == AMBIGUOUS else DELETION
