"""
LinkedIn network tools.

Provides read-only listing of pending connection invitations. Accepting,
ignoring and withdrawing invitations are not exposed here.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors

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
