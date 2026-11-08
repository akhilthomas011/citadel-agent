"""Release checks reject unknown metadata and never treat template configuration as enforcement."""

import copy

from citadel_agent.portal.release import branch_gate, environment_gate


def protected_branch():
    return {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
        },
        "required_status_checks": {"contexts": ["validate", "test"]},
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }


def protected_environment():
    return {
        "can_admins_bypass": False,
        "deployment_branch_policy": {"protected_branches": True},
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "Team", "reviewer": {"id": 123}}],
            }
        ],
    }


def test_missing_or_unreadable_remote_metadata_is_never_pass():
    assert branch_gate(None, ("test",)).status == "unknown"
    assert environment_gate(None).status == "unknown"
    assert branch_gate({}, ("test",)).status == "blocked"
    assert environment_gate({}).status == "blocked"


def test_branch_requires_independent_review_and_exact_selected_checks():
    branch = protected_branch()
    assert branch_gate(branch, ("validate", "test")).status == "pass"
    assert branch_gate(branch, ()).status == "blocked"
    assert branch_gate(branch, ("never-configured-check",)).status == "blocked"
    branch["required_pull_request_reviews"]["required_approving_review_count"] = 0
    assert branch_gate(branch, ("test",)).status == "blocked"


def test_stale_review_and_destructive_push_exceptions_block_release():
    branch = protected_branch()
    branch["required_pull_request_reviews"]["dismiss_stale_reviews"] = False
    assert branch_gate(branch, ("test",)).status == "blocked"
    branch = protected_branch()
    branch["allow_force_pushes"]["enabled"] = True
    assert branch_gate(branch, ("test",)).status == "blocked"


def test_production_environment_cannot_bypass_or_self_approve():
    env = protected_environment()
    assert environment_gate(env).status == "pass"
    for field in ("can_admins_bypass",):
        broken = copy.deepcopy(env)
        broken[field] = True
        assert environment_gate(broken).status == "blocked"
    env["protection_rules"][0]["prevent_self_review"] = False
    assert environment_gate(env).status == "blocked"
