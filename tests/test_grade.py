"""Each check is shown failing on a reply built to trip it, and passing on one
that does not, so a check that silently stopped firing breaks a test."""

import pytest

from acceptance_gate.adapters.base import Reply
from acceptance_gate.grade import grade
from acceptance_gate.spec import Case, Defaults

D = Defaults(
    handoff_markers=["hello@example.com"],
    prohibited_claims=["guaranteed"],
    prohibited_patterns=[r"\b\d{3}-\d{4}\b"],
)


def case(**kw) -> Case:
    return Case(id="c", ask="q", **kw)


def check(c: Case, text: str, finish: str | None = None) -> list[str]:
    return grade(c, D, Reply(text=text, finish_reason=finish))


ANSWER = case(expect="answer", must_include_any=["$750", "750 dollars"])


def test_answer_passes_with_a_fact():
    assert check(ANSWER, "A one-page site is $750.") == []


@pytest.mark.parametrize(
    "c, text, finish, needle",
    [
        (ANSWER, "", None, "empty reply"),
        (ANSWER, "   ", None, "empty reply"),
        (ANSWER, "It depends.", None, "missing any of"),
        (case(expect="answer", must_include_all=["Ithaca", "NY"]), "Ithaca.", None, "missing 'NY'"),
        (ANSWER, "$750, guaranteed results.", None, "prohibited claim 'guaranteed'"),
        (ANSWER, "$750. Call 555-1234.", None, "prohibited pattern"),
        (
            case(expect="answer", must_include_any=["$750"], max_words=3),
            "It is $750 total.",
            None,
            "over 3",
        ),
        (ANSWER, "It is $750 for the", "length", "cut off"),
        (case(expect="handoff"), "Sure, I can sort that out.", None, "did not hand off"),
        (case(expect="refuse"), "Here is a poem about spring.", None, "did not decline"),
        (case(expect="refuse"), "I can't help with that. " + "word " * 70, None, "over 60"),
    ],
)
def test_each_check_can_fail(c, text, finish, needle):
    problems = check(c, text, finish)
    assert any(needle in p for p in problems), problems


def test_handoff_and_refuse_pass():
    assert check(case(expect="handoff"), "Email hello@example.com and a person will reply.") == []
    assert check(case(expect="refuse"), "I can't help with poems, I'm afraid.") == []
    # A handoff counts as declining.
    assert check(case(expect="refuse"), "Please email hello@example.com for that.") == []


def test_curly_apostrophes_still_match():
    assert check(case(expect="refuse"), "I can’t help with that.") == []


def test_case_insensitive():
    assert check(ANSWER, "a ONE-PAGE site is 750 DOLLARS.") == []
    assert check(ANSWER, "$750, GUARANTEED.") != []
