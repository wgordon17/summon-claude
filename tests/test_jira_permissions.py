"""Guard tests for Jira MCP permission gating.

SC-04: Deny list checked before auto-approve (ordering invariant).
SC-05: All 31 known Jira tools are classified.
SC-06: No hard-deny tool matches an auto-approve prefix.
"""

from __future__ import annotations

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_slack_client
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import (
    _JIRA_MCP_AUTO_APPROVE_EXACT,
    _JIRA_MCP_AUTO_APPROVE_PREFIXES,
    _JIRA_MCP_HARD_DENY,
    _JIRA_MCP_PREFIX,
    ApprovalBridge,
    PermissionHandler,
)
from summon_claude.slack.router import ThreadRouter

# All 31 known Jira MCP tool names from spike-findings.json (prefixed with mcp__jira__)
_ALL_KNOWN_JIRA_TOOLS = frozenset(
    {
        "mcp__jira__addCommentToJiraIssue",
        "mcp__jira__addWorklogToJiraIssue",
        "mcp__jira__atlassianUserInfo",
        "mcp__jira__createConfluenceFooterComment",
        "mcp__jira__createConfluenceInlineComment",
        "mcp__jira__createConfluencePage",
        "mcp__jira__createIssueLink",
        "mcp__jira__createJiraIssue",
        "mcp__jira__editJiraIssue",
        "mcp__jira__fetchAtlassian",
        "mcp__jira__getAccessibleAtlassianResources",
        "mcp__jira__getConfluenceCommentChildren",
        "mcp__jira__getConfluencePage",
        "mcp__jira__getConfluencePageDescendants",
        "mcp__jira__getConfluencePageFooterComments",
        "mcp__jira__getConfluencePageInlineComments",
        "mcp__jira__getConfluenceSpaces",
        "mcp__jira__getIssueLinkTypes",
        "mcp__jira__getJiraIssue",
        "mcp__jira__getJiraIssueRemoteIssueLinks",
        "mcp__jira__getJiraIssueTypeMetaWithFields",
        "mcp__jira__getJiraProjectIssueTypesMetadata",
        "mcp__jira__getPagesInConfluenceSpace",
        "mcp__jira__getTransitionsForJiraIssue",
        "mcp__jira__getVisibleJiraProjects",
        "mcp__jira__lookupJiraAccountId",
        "mcp__jira__searchAtlassian",
        "mcp__jira__searchConfluenceUsingCql",
        "mcp__jira__searchJiraIssuesUsingJql",
        "mcp__jira__transitionJiraIssue",
        "mcp__jira__updateConfluencePage",
    }
)


def make_handler(bridge=None):
    """Create a PermissionHandler with a mocked ThreadRouter."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    config = SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "abcd1234",
            "permission_debounce_ms": 10,
        }
    )
    return PermissionHandler(router, config, authenticated_user_id="U_TEST", bridge=bridge)


class TestJiraMCPConstantsPinned:
    """Guard tests: pin permission sets so changes aren't silently missed."""

    def test_hard_deny_set_pinned(self):
        """Pin the exact contents of _JIRA_MCP_HARD_DENY (SC-04)."""
        assert (
            frozenset(
                {
                    "mcp__jira__addCommentToJiraIssue",
                    "mcp__jira__addWorklogToJiraIssue",
                    "mcp__jira__createConfluenceFooterComment",
                    "mcp__jira__createConfluenceInlineComment",
                    "mcp__jira__createConfluencePage",
                    "mcp__jira__createIssueLink",
                    "mcp__jira__createJiraIssue",
                    "mcp__jira__editJiraIssue",
                    "mcp__jira__fetchAtlassian",
                    "mcp__jira__transitionJiraIssue",
                    "mcp__jira__updateConfluencePage",
                }
            )
            == _JIRA_MCP_HARD_DENY
        )

    def test_auto_approve_prefixes_pinned(self):
        """Pin the exact contents of _JIRA_MCP_AUTO_APPROVE_PREFIXES."""
        assert _JIRA_MCP_AUTO_APPROVE_PREFIXES == (
            "mcp__jira__get",
            "mcp__jira__search",
            "mcp__jira__lookup",
        )

    def test_auto_approve_exact_pinned(self):
        """Pin the exact contents of _JIRA_MCP_AUTO_APPROVE_EXACT."""
        assert frozenset({"mcp__jira__atlassianUserInfo"}) == _JIRA_MCP_AUTO_APPROVE_EXACT


class TestJiraMCPInvariants:
    """Invariant tests to catch structural regressions."""

    def test_every_prefix_starts_with_jira_mcp_prefix(self):
        """All auto-approve prefixes must start with the Jira MCP prefix."""
        for prefix in _JIRA_MCP_AUTO_APPROVE_PREFIXES:
            assert prefix.startswith(_JIRA_MCP_PREFIX), (
                f"Auto-approve prefix '{prefix}' does not start with '{_JIRA_MCP_PREFIX}'"
            )

    def test_every_exact_tool_starts_with_jira_mcp_prefix(self):
        """All exact-match tools must start with the Jira MCP prefix."""
        for tool in _JIRA_MCP_AUTO_APPROVE_EXACT:
            assert tool.startswith(_JIRA_MCP_PREFIX), (
                f"Exact-match tool '{tool}' does not start with '{_JIRA_MCP_PREFIX}'"
            )

    def test_every_hard_deny_tool_starts_with_jira_mcp_prefix(self):
        """All hard-deny tools must start with the Jira MCP prefix."""
        for tool in _JIRA_MCP_HARD_DENY:
            assert tool.startswith(_JIRA_MCP_PREFIX), (
                f"Hard-deny tool '{tool}' does not start with '{_JIRA_MCP_PREFIX}'"
            )

    def test_no_hard_deny_tool_matches_auto_approve_prefix(self):
        """SEC-017 / SC-06: No hard-deny tool should match an auto-approve prefix.

        If this fails, a write tool could be auto-approved if the ordering
        check is ever reversed.
        """
        for tool in _JIRA_MCP_HARD_DENY:
            assert not tool.startswith(_JIRA_MCP_AUTO_APPROVE_PREFIXES), (
                f"Hard-deny tool '{tool}' matches an auto-approve prefix"
            )

    def test_all_known_tools_classified(self):
        """SC-05: Every known Jira tool must be in exactly one category.

        Any tool not in hard-deny, auto-approve-prefix, or auto-approve-exact
        would fall through to the fail-closed deny at step 2d-iv. This test
        ensures the 31 known tools are all explicitly classified.
        """
        unclassified = []
        for tool in _ALL_KNOWN_JIRA_TOOLS:
            in_hard_deny = tool in _JIRA_MCP_HARD_DENY
            in_prefix = tool.startswith(_JIRA_MCP_AUTO_APPROVE_PREFIXES)
            in_exact = tool in _JIRA_MCP_AUTO_APPROVE_EXACT
            if not (in_hard_deny or in_prefix or in_exact):
                unclassified.append(tool)

        assert not unclassified, (
            f"These Jira tools are not classified (would fall through to fail-closed deny): "
            f"{sorted(unclassified)}"
        )

    def test_known_tool_count_matches_spike(self):
        """The known tool list must match the 31 tools from spike-findings.json.

        spike-findings.json tool_names has 31 entries (verified 2026-03-31).
        """
        assert len(_ALL_KNOWN_JIRA_TOOLS) == 31


class TestJiraMCPBehavioral:
    """Behavioral tests: verify the permission handler returns the right decisions."""

    async def test_get_jira_issue_auto_approved(self):
        """mcp__jira__getJiraIssue must be auto-approved (prefix match)."""
        handler = make_handler()
        result = await handler.handle(
            "mcp__jira__getJiraIssue",
            {"cloudId": "x", "issueKey": "PROJ-1"},
            None,
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_search_jira_issues_auto_approved(self):
        """mcp__jira__searchJiraIssuesUsingJql must be auto-approved (prefix match)."""
        handler = make_handler()
        result = await handler.handle(
            "mcp__jira__searchJiraIssuesUsingJql",
            {"cloudId": "x", "jql": "assignee = currentUser()"},
            None,
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_lookup_jira_account_auto_approved(self):
        """mcp__jira__lookupJiraAccountId must be auto-approved (prefix match)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__lookupJiraAccountId", {"query": "gordon"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_atlassian_user_info_auto_approved(self):
        """mcp__jira__atlassianUserInfo must be auto-approved (exact match)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__atlassianUserInfo", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_create_jira_issue_hard_denied(self):
        """mcp__jira__createJiraIssue must be hard-denied (write tool)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__createJiraIssue", {"cloudId": "x"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "read-only" in result.message.lower()

    async def test_edit_jira_issue_hard_denied(self):
        """mcp__jira__editJiraIssue must be hard-denied (write tool)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__editJiraIssue", {"cloudId": "x"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_fetch_atlassian_hard_denied(self):
        """mcp__jira__fetchAtlassian must be hard-denied (generic escape hatch — SEC-008)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__fetchAtlassian", {"ari": "ari:cloud:..."}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "read-only" in result.message.lower()

    async def test_add_comment_hard_denied(self):
        """mcp__jira__addCommentToJiraIssue must be hard-denied."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__addCommentToJiraIssue", {"cloudId": "x"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_transition_jira_issue_hard_denied(self):
        """mcp__jira__transitionJiraIssue must be hard-denied (write tool)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__transitionJiraIssue", {"cloudId": "x"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_unknown_jira_tool_fail_closed(self):
        """An unknown Jira tool must be denied (fail-closed at step 2d-iv)."""
        handler = make_handler()
        result = await handler.handle("mcp__jira__unknownNewTool", {}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "fail-closed" in result.message.lower()

    async def test_unknown_jira_tool_with_get_prefix_auto_approved(self):
        """A future Jira tool with a 'get' prefix is auto-approved by prefix match.

        The SEC-017 ordering invariant ensures that if Atlassian ever adds a write
        tool with a 'get' prefix, adding it to _JIRA_MCP_HARD_DENY (checked first)
        will deny it before the prefix check fires. The guard test
        test_no_hard_deny_tool_matches_auto_approve_prefix detects violations.
        """
        handler = make_handler()
        result = await handler.handle("mcp__jira__getFutureReadTool", {}, None)
        assert isinstance(result, PermissionResultAllow)


class TestJiraBridgeResolution:
    """Tests that Jira permission paths resolve the ApprovalBridge correctly."""

    async def test_hard_deny_resolves_bridge_with_denial(self):
        """Hard-denied Jira tool resolves bridge with denial label."""
        bridge = ApprovalBridge()
        handler = make_handler(bridge=bridge)
        result = await handler.handle("mcp__jira__addCommentToJiraIssue", {"cloudId": "x"}, None)
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("mcp__jira__addCommentToJiraIssue")
        assert fut.done()
        info = fut.result()
        assert info.label == "denied"
        assert info.is_denial is True
        assert info.reason == "read-only mode"

    async def test_unknown_jira_tool_resolves_bridge_with_denial(self):
        """Unknown Jira tool (fail-closed) resolves bridge with denial label."""
        bridge = ApprovalBridge()
        handler = make_handler(bridge=bridge)
        result = await handler.handle("mcp__jira__unknownNewTool", {}, None)
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("mcp__jira__unknownNewTool")
        assert fut.done()
        info = fut.result()
        assert info.label == "denied"
        assert info.is_denial is True
        assert info.reason == "read-only mode"
