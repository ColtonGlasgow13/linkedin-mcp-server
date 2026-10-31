"""
LinkedIn network tools.

Lists pending connection invitations and withdraws one sent invitation at a
time. Accepting and ignoring received invitations are not exposed here.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier

logger = logging.getLogger(__name__)


def register_network_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all network-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Pending Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network"},
    )
    @tool_errors
    async def get_pending_invitations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
        kind: Literal["received", "sent"] = "received",
    ) -> dict[str, Any]:
        """
        List pending LinkedIn connection invitations, received or sent.

        Reads /mynetwork/invitation-manager/{received|sent}/. A sent invitation
        that is no longer listed was accepted or withdrawn; LinkedIn shows a
        declined one as still pending. Truncated invitation notes are expanded
        before reading. Read-only: nothing is accepted, ignored or withdrawn.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of invitations to return (1-500, default 20).
                References are capped exactly; the text is trimmed at the first
                omitted invitation.
            kind: "received" (default) for invitations to you, "sent" for your
                outgoing invitations awaiting a response.

        Returns:
            Dict with url, sections (invitations -> raw text), and optional
            references (invitations -> the people's profile links, in page
            order).
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_pending_invitations")
        logger.info("Fetching pending invitations (kind=%s, limit=%d)", kind, limit)

        await ctx.report_progress(
            progress=0, total=100, message=f"Loading {kind} invitations"
        )

        result = await extractor.get_pending_invitations(limit=limit, kind=kind)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Withdraw Invitation",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"network", "actions"},
    )
    @tool_errors
    async def withdraw_invitation(
        linkedin_username: str,
        ctx: Context,
        confirm_withdraw: bool = False,
    ) -> dict[str, Any]:
        """
        Withdraw the pending connection invitation you sent to one person.

        Opens the person's profile and checks that an invitation is pending.
        With confirm_withdraw False (the default) it stops there and reports
        what it found. With confirm_withdraw True it clicks Pending, confirms
        the withdraw dialog, and re-reads the profile to check the invitation
        is gone. LinkedIn does not let you invite the same person again for
        about three weeks after a withdrawal. One person per call.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            confirm_withdraw: Must be True to withdraw; False only checks

        Returns:
            Dict with url, status, message, withdrawn, and the profile text.
            Statuses: pending (an invitation is pending and confirm_withdraw
            was False), not_pending (no outstanding invitation; the message
            names the profile's state), withdrawn, withdraw_failed (nothing
            withdrawn, or the profile still reads as pending after
            confirming), or unavailable.

            A status of ``outcome_unknown`` comes from the transport rather
            than the page: the call's answer never arrived from the browser
            process. It carries ``retry_safe: False``; repeating the call is
            harmless, because a withdrawn invitation reads as not_pending.
        """
        linkedin_username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="withdraw_invitation")
        logger.info(
            "Withdrawing invitation to %s (confirm_withdraw=%s)",
            linkedin_username,
            confirm_withdraw,
        )

        await ctx.report_progress(
            progress=0, total=100, message="Checking the pending invitation"
        )

        result = await extractor.withdraw_invitation(
            linkedin_username, confirm_withdraw=confirm_withdraw
        )

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result
