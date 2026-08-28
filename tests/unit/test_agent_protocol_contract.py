from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "docs" / "AGENT_PROTOCOL.md"


def _protocol_text() -> str:
    return PROTOCOL.read_text(encoding="utf-8")


def test_post_merge_proof_starts_from_the_merge_result() -> None:
    text = _protocol_text()
    required_fragments = (
        "baseRefName,headRefOid,mergeCommit,state",
        "git merge-base --is-ancestor <merge-result-sha> origin/<default>",
        "exact default-branch CI",
    )

    for fragment in required_fragments:
        assert fragment in text, f"agent protocol omits merge-result proof: {fragment}"


def test_merge_strategy_api_claims_cite_official_github_sources() -> None:
    text = _protocol_text()
    required_sources = (
        "https://docs.github.com/en/graphql/reference/pulls#pullrequest",
        "https://docs.github.com/en/pull-requests/reference/pull-request-merges",
    )

    for source in required_sources:
        assert source in text, f"agent protocol omits official GitHub source: {source}"


def test_rewritten_candidates_use_changed_path_equality() -> None:
    text = _protocol_text()
    required_fragments = (
        "squash",
        "rebase",
        "candidate ancestry is expected to fail",
        "git diff --exit-code <candidate-sha> <merge-result-sha> -- <changed-paths>",
        "gh api repos/Ayyitskevin/Chronos/pulls/<n>/files",
        ".previous_filename // empty",
    )

    for fragment in required_fragments:
        assert fragment in text, f"agent protocol omits rewritten-candidate proof: {fragment}"


def test_preserved_candidates_still_require_ancestry() -> None:
    text = _protocol_text()

    assert "git merge-base --is-ancestor <candidate-sha> <merge-result-sha>" in text


def test_approved_stacks_retarget_each_descendant_before_merge() -> None:
    text = " ".join(_protocol_text().split())

    assert "Stacked review may be owner-approved; stacked landing is not" in text
    assert "retarget every descendant to the live default before merging it" in text


def test_protocol_does_not_claim_candidate_ancestry_is_universal() -> None:
    text = _protocol_text()
    forbidden_patterns = (
        r"after the final merge,\s+prove ancestry.*?for every PR in the stack",
        r"every merge\s+is verified by ancestry",
    )

    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is None, (
            f"agent protocol still treats candidate ancestry as universal: {pattern}"
        )
