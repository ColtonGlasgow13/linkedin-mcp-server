"""Tests for the pending-invitation reader.

The reader only reads, apart from expanding truncated notes in place. The
expansion program never executes under a mock, so its guards are pinned on the
program text; everything else is checked through what the reader returns.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.linkedin import invitations
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.invitations import (
    InvitationReader,
    trim_invitation_text,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession


def _reader(page: Any) -> InvitationReader:
    """Wire the invitation reader the way the facade does."""
    session = PageSession(page)
    return InvitationReader(session, PageNavigator(session), PageContentReader(session))


def _person(index: int) -> dict[str, str]:
    return {
        "href": f"https://www.linkedin.com/in/user-{index}/",
        "text": f"User {index}",
    }


def _card_text(count: int) -> str:
    return "\n\n".join(f"User {i}\nSent {i + 1} days ago" for i in range(count))


@pytest.fixture
def page_reads():
    """Stand in for every page boundary; ``root`` and ``zero`` steer the read."""
    root = AsyncMock(return_value={"source": "root", "text": "", "references": []})
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock) as nav,
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
        patch.object(
            InvitationReader, "_load_more_cards", new_callable=AsyncMock
        ) as load_more,
        patch.object(InvitationReader, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(
            InvitationReader, "_expand_invitation_notes", new_callable=AsyncMock
        ),
        patch.object(
            InvitationReader,
            "_received_count_is_zero",
            new_callable=AsyncMock,
            return_value=False,
        ) as zero,
        patch.object(
            InvitationReader, "_card_names", new_callable=AsyncMock, return_value={}
        ) as card_names,
        patch.object(PageContentReader, "_extract_root_content", root),
    ):
        yield SimpleNamespace(
            nav=nav, load_more=load_more, zero=zero, root=root, card_names=card_names
        )


def _serve(page_reads: SimpleNamespace, count: int) -> None:
    page_reads.root.return_value = {
        "source": "root",
        "text": _card_text(count),
        "references": [_person(i) for i in range(count)],
    }


class TestGetPendingInvitations:
    async def test_sent_reads_the_sent_manager(self, mock_page, page_reads):
        _serve(page_reads, 2)

        result = await _reader(mock_page).get_pending_invitations(kind="sent")

        sent_url = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
        page_reads.nav.assert_awaited_once_with(sent_url)
        assert result["url"] == sent_url
        assert [ref["url"] for ref in result["references"]["invitations"]] == [
            "/in/user-0/",
            "/in/user-1/",
        ]

    async def test_references_and_text_stop_at_the_limit(self, mock_page, page_reads):
        _serve(page_reads, 5)

        result = await _reader(mock_page).get_pending_invitations(limit=2, kind="sent")

        assert len(result["references"]["invitations"]) == 2
        assert result["sections"]["invitations"] == (
            "User 0\nSent 1 days ago\n\nUser 1\nSent 2 days ago"
        )

    async def test_more_than_the_default_reference_cap_survives(
        self, mock_page, page_reads
    ):
        # Without a section cap the default of 12 drops the rest silently, and
        # a sender reconciling pending invitations would read them as accepted.
        _serve(page_reads, 40)

        result = await _reader(mock_page).get_pending_invitations(limit=40, kind="sent")

        assert len(result["references"]["invitations"]) == 40

    async def test_loads_more_pages_for_the_requested_limit(
        self, mock_page, page_reads
    ):
        _serve(page_reads, 1)

        await _reader(mock_page).get_pending_invitations(limit=35, kind="sent")

        page_reads.load_more.assert_awaited_once_with(35)

    async def test_received_zero_count_skips_recommendations(
        self, mock_page, page_reads
    ):
        _serve(page_reads, 3)
        page_reads.zero.return_value = True

        result = await _reader(mock_page).get_pending_invitations()

        assert result == {
            "url": "https://www.linkedin.com/mynetwork/invitation-manager/received/",
            "sections": {},
        }
        page_reads.root.assert_not_awaited()

    async def test_sent_never_consults_the_received_count(self, mock_page, page_reads):
        _serve(page_reads, 1)
        page_reads.zero.return_value = True

        result = await _reader(mock_page).get_pending_invitations(kind="sent")

        assert "invitations" in result["sections"]
        page_reads.zero.assert_not_awaited()

    async def test_photo_only_profile_links_take_the_card_name(
        self, mock_page, page_reads
    ):
        # Live sent page, 2026-10-21: every card's /in/ link wraps only the
        # photo, so without the card name no reference survived at all.
        fixture = json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "linkedin"
                / "invitations_sent_photo_only_anchors.json"
            ).read_text()
        )
        page_reads.root.return_value = fixture["root"]
        page_reads.card_names.return_value = fixture["card_names"]

        result = await _reader(mock_page).get_pending_invitations(limit=2, kind="sent")

        assert [
            (ref["url"], ref["text"]) for ref in result["references"]["invitations"]
        ] == [("/in/alex-doe-1a2b3c/", "Alex Doe"), ("/in/samroe/", "Sam Roe")]
        assert "Kim Poe" not in result["sections"]["invitations"]

    async def test_photo_only_links_without_a_card_name_are_dropped(
        self, mock_page, page_reads
    ):
        page_reads.root.return_value = {
            "source": "root",
            "text": "Someone\nSent today",
            "references": [
                {"href": "https://www.linkedin.com/in/someone/", "text": ""}
            ],
        }

        result = await _reader(mock_page).get_pending_invitations(kind="sent")

        assert "references" not in result

    async def test_an_empty_page_returns_no_section(self, mock_page, page_reads):
        result = await _reader(mock_page).get_pending_invitations(kind="sent")

        assert result["sections"] == {}
        assert "references" not in result


class TestLoadMoreCards:
    """The manager pages with a button, ten cards a click."""

    @staticmethod
    def _run(*answers: Any) -> AsyncMock:
        return AsyncMock(side_effect=list(answers))

    async def test_clicks_until_the_limit_could_be_met(self, mock_page):
        # 35 wanted: 10 on load, then three clicks reach 40.
        run = self._run(100, 200, 200, 300, 300, 400)
        with (
            patch.object(PageSession, "run_on_linkedin", run),
            patch.object(PageSession, "delay", new_callable=AsyncMock) as delay,
        ):
            loaded = await _reader(mock_page)._load_more_cards(35)

        assert loaded == 3
        assert run.await_count == 6
        assert delay.await_count == 3

    async def test_stops_when_the_button_is_gone(self, mock_page):
        run = self._run(100, 200, None)
        with (
            patch.object(PageSession, "run_on_linkedin", run),
            patch.object(PageSession, "delay", new_callable=AsyncMock),
        ):
            loaded = await _reader(mock_page)._load_more_cards(500)

        assert loaded == 1
        assert run.await_count == 3

    async def test_stops_when_a_click_loads_nothing(self, mock_page):
        run = self._run(100, 100)
        with (
            patch.object(PageSession, "run_on_linkedin", run),
            patch.object(PageSession, "delay", new_callable=AsyncMock),
        ):
            loaded = await _reader(mock_page)._load_more_cards(500)

        assert loaded == 0
        assert run.await_count == 2

    async def test_a_single_page_limit_never_clicks(self, mock_page):
        run = self._run()
        with patch.object(PageSession, "run_on_linkedin", run):
            assert await _reader(mock_page)._load_more_cards(10) == 0

        run.assert_not_awaited()


class TestTrimInvitationText:
    @staticmethod
    def _refs(*labels: str) -> list[Reference]:
        return [
            {"kind": "person", "url": f"/in/{label.lower()}/", "text": label}
            for label in labels
        ]

    def test_cuts_before_the_first_omitted_label(self):
        text = "Ada\nnote mentions Cy\nBob\nhi\nCy\nhey"

        # The search resumes after the last kept label, so the mention of Cy in
        # Ada's note is not mistaken for Cy's card.
        assert trim_invitation_text(text, self._refs("Ada", "Bob", "Cy"), 2) == (
            "Ada\nnote mentions Cy\nBob\nhi"
        )

    def test_falls_back_to_blank_line_cards(self):
        text = "Ada\nhi\n\nSomeone unlinked\nhey"

        assert trim_invitation_text(text, self._refs("Ada"), 1) == "Ada\nhi"

    def test_keeps_text_within_the_limit(self):
        text = "Ada\nhi\n\nBob\nhey"

        assert trim_invitation_text(text, self._refs("Ada", "Bob"), 5) == text


class TestCardNamesProgram:
    def test_card_names_are_read_structurally(self):
        script = invitations._CARD_NAMES_JS
        assert '[role="listitem"]' in script
        assert 'a[href*="/in/"]' in script


class TestNoteExpansionProgram:
    def test_guards_against_recollapsing_and_locale_text(self):
        js = invitations._EXPAND_NOTES_JS

        assert '[data-testid="expandable-text-button"]' in js
        assert ':not([aria-expanded="true"])' in js
        assert ":not([data-mcp-clicked])" in js
        # Mark only after dispatch succeeds, so a failed click is retried.
        assert js.index("btn.dispatchEvent") < js.index("btn.dataset.mcpClicked")

    async def test_stops_after_a_pass_that_expands_nothing(self, mock_page):
        reader = _reader(mock_page)
        with patch.object(
            PageSession, "run_on_linkedin", AsyncMock(side_effect=[0])
        ) as run:
            await reader._expand_invitation_notes()

        assert run.await_count == 1

    async def test_runs_a_second_pass_for_lazy_loaded_cards(self, mock_page):
        reader = _reader(mock_page)
        with patch.object(
            PageSession, "run_on_linkedin", AsyncMock(side_effect=[3, 1])
        ) as run:
            await reader._expand_invitation_notes()

        assert run.await_count == 2
