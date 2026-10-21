"""Pending network invitation listing (received or sent)."""

from __future__ import annotations

from typing import Any, Literal

import logging
import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.link_metadata import (
    RawReference,
    Reference,
    build_references,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import strip_linkedin_noise

logger = logging.getLogger(__name__)

InvitationKind = Literal["received", "sent"]

_SECTION = "invitations"

# Collapsed invitation notes render an inline toggle carrying this testid. The
# testid is the locale-independent signal; the visible verb ("see more",
# "voir plus") is not. The toggle ships with `pointer-events: none` and
# `aria-hidden`, so a normal click never reaches React's handler: the script
# re-enables pointer events on these nodes only and dispatches a bubbling
# click. Clicked toggles are marked so a second pass, which picks up cards the
# first pass lazy-loaded, never re-collapses a note it just expanded; the
# marker is set after dispatch so a failed click stays eligible for retry.
# Toggles already `aria-expanded="true"` on load are left alone.
_EXPAND_NOTES_JS = """async () => {
    if (!document.getElementById('__linkedin_mcp_expand_style')) {
        const styleNode = document.createElement('style');
        styleNode.id = '__linkedin_mcp_expand_style';
        styleNode.textContent = `
            [data-testid="expandable-text-button"],
            [data-testid="expandable-text-box"] {
                pointer-events: auto !important;
            }
        `;
        document.head.appendChild(styleNode);
    }
    const buttons = Array.from(document.querySelectorAll(
        'main [data-testid="expandable-text-button"]'
        + ':not([aria-expanded="true"])'
        + ':not([data-mcp-clicked])'
    ));
    let count = 0;
    for (const btn of buttons) {
        try {
            btn.dispatchEvent(new MouseEvent('click', {
                bubbles: true,
                cancelable: true,
                view: window,
            }));
            btn.dataset.mcpClicked = '1';
            count++;
        } catch (e) {
            /* leave unmarked so a later pass can retry */
        }
    }
    await new Promise(r => setTimeout(r, 400));
    return count;
}"""

# The received manager renders "people you may know" cards below its empty
# state, and their anchors are not invitations. The selected ALL tab is found
# structurally (current-link state plus its stable URL); the count inside it is
# the only text read, and digits in parentheses are locale-independent.
_RECEIVED_COUNT_IS_ZERO_JS = r"""() => {
    const currentAll = document.querySelector(
        'main a[aria-current="true"]'
        + '[href*="/mynetwork/invitation-manager/received/ALL"]'
    );
    if (!currentAll) return false;
    const text = currentAll.innerText || currentAll.textContent || '';
    const match = text.match(/\((\d+)\)/);
    return match ? Number(match[1]) === 0 : false;
}"""


# Each card's profile link wraps only the photo, so it has no text, aria-label
# or title and the generic reference builder drops it (measured live on the sent
# page 2026-10-21: 84 cards, 84 photo-only /in/ anchors, zero references). The
# card is the anchor's closest role="listitem" and its first rendered line is
# the person's name; both are structural, so no locale text is read. Anchors
# that already carry text are left to the generic builder.
_CARD_NAMES_JS = """() => {
    const main = document.querySelector('main');
    if (!main) return {};
    const names = {};
    for (const anchor of main.querySelectorAll('a[href*="/in/"]')) {
        if ((anchor.innerText || '').trim()) continue;
        const card = anchor.closest('[role="listitem"]');
        if (!card) continue;
        const name = (card.innerText || '')
            .split('\\n')
            .map(line => line.trim())
            .find(Boolean);
        if (name && !(anchor.href in names)) names[anchor.href] = name;
    }
    return names;
}"""


# The manager renders ten cards and a "Load more" button, not an infinite
# scroll: measured live on the sent page 2026-10-12 (45 pending, 10 rendered
# after scrolling). The button carries no attribute that tells it apart from
# the card buttons, so its visible text is the only signal. The browser locale
# is forced to en-US (see BrowserManager); any other locale finds no button
# and the read stops at the first ten cards rather than clicking a guess.
_LOAD_MORE_LABELS = ("Load more", "Show more")
_LOAD_MORE_JS = """({ labels }) => {
    const main = document.querySelector('main');
    if (!main) return null;
    const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
    const button = Array.from(main.querySelectorAll('button')).find(
        candidate => labels.includes(normalize(candidate.innerText))
            && !candidate.disabled
    );
    if (!button) return null;
    const before = main.innerText.length;
    button.click();
    return before;
}"""
_MAIN_TEXT_LENGTH_JS = """() => {
    const main = document.querySelector('main');
    return main ? main.innerText.length : 0;
}"""
# Cards per "Load more" page, measured with the limit above.
_CARDS_PER_PAGE = 10
_LOAD_MORE_PAUSE = 1.5


def invitation_manager_url(kind: InvitationKind) -> str:
    """Return the invitation-manager page for one direction."""
    return f"https://www.linkedin.com/mynetwork/invitation-manager/{kind}/"


def trim_invitation_text(text: str, references: list[Reference], limit: int) -> str:
    """Cut invitation text so it describes the same cards as ``references[:limit]``.

    LinkedIn lazy-loads cards a screenful at a time, so even ``limit=1`` renders
    several. References keep DOM order and each card's text carries its
    person's name, so the cut goes just before the first omitted reference's
    label. When later cards yield no usable reference, the blank lines LinkedIn
    puts between cards are the fallback boundary.
    """
    if limit < 1 or not text:
        return text

    search_from = 0
    for reference in references[:limit]:
        label = reference.get("text")
        if not label:
            continue
        index = text.find(label, search_from)
        if index >= 0:
            search_from = index + len(label)

    if len(references) > limit:
        next_label = references[limit].get("text")
        if next_label:
            next_index = text.find(next_label, search_from)
            if next_index > 0:
                return text[:next_index].rstrip()

    first_label = next(
        (reference.get("text") for reference in references if reference.get("text")),
        None,
    )
    if not first_label:
        return text
    first_index = text.find(first_label)
    if first_index < 0:
        return text

    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", text[first_index:].strip())
        if block.strip()
    ]
    if len(blocks) <= limit:
        return text
    return (text[:first_index] + "\n\n".join(blocks[:limit])).rstrip()


class InvitationReader:
    """List pending connection invitations from the invitation manager.

    Read-only: nothing here accepts, ignores or withdraws an invitation. The
    only clicks expand truncated invitation notes in place.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

    async def _wait_for_main_text(self, *, timeout: int = 10000) -> None:
        """Wait for main to hold enough text to read."""
        try:
            await self._session.page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    return Boolean(main) && main.innerText.length > 100;
                }""",
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("Invitation manager content did not appear")

    async def _expand_invitation_notes(self) -> None:
        """Expand truncated invitation notes, in two passes at most."""
        try:
            for _ in range(2):
                if not await self._session.run_on_linkedin(_EXPAND_NOTES_JS):
                    break
        except Exception as exc:
            logger.debug("Failed to expand invitation notes: %s", exc)

    async def _received_count_is_zero(self) -> bool:
        """Return True when the selected received ALL tab counts zero."""
        try:
            return bool(await self._session.run_on_linkedin(_RECEIVED_COUNT_IS_ZERO_JS))
        except Exception as exc:
            logger.debug("Failed to read received invitation count: %s", exc)
            return False

    async def _card_names(self) -> dict[str, str]:
        """Map each textless profile link to its card's name."""
        try:
            names = await self._session.run_on_linkedin(_CARD_NAMES_JS)
        except Exception as exc:
            logger.debug("Failed to read invitation card names: %s", exc)
            return {}
        return names if isinstance(names, dict) else {}

    async def _load_more_cards(self, limit: int) -> int:
        """Click "Load more" until ``limit`` cards could be on the page.

        Stops early when the button is gone or a click adds no text. Returns
        the number of clicks that loaded something.
        """
        loaded = 0
        for _ in range(max(0, -(-limit // _CARDS_PER_PAGE) - 1)):
            before = await self._session.run_on_linkedin(
                _LOAD_MORE_JS, {"labels": list(_LOAD_MORE_LABELS)}
            )
            if before is None:
                break
            await self._session.delay(_LOAD_MORE_PAUSE)
            after = await self._session.run_on_linkedin(_MAIN_TEXT_LENGTH_JS)
            if not isinstance(after, int) or after <= before:
                break
            loaded += 1
        return loaded

    async def get_pending_invitations(
        self, limit: int = 20, kind: InvitationKind = "received"
    ) -> dict[str, Any]:
        """List pending invitations in one direction.

        ``limit`` caps the returned references exactly and sets how many
        "Load more" pages are fetched; the section text is trimmed to match.
        """
        url = invitation_manager_url(kind)
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._wait_for_main_text()
        await self._session.dismiss_modal()

        await self._load_more_cards(limit)
        await self._expand_invitation_notes()

        result: dict[str, Any] = {"url": url, "sections": {}}
        if kind == "received" and await self._received_count_is_zero():
            return result

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        if not cleaned:
            return result

        names = await self._card_names()
        raw_references: list[RawReference] = []
        for raw in raw_result["references"]:
            name = names.get(raw.get("href", ""))
            if name and not raw.get("text"):
                raw = RawReference(**{**raw, "text": name})
            raw_references.append(raw)
        all_references = build_references(raw_references, _SECTION)
        result["sections"][_SECTION] = trim_invitation_text(
            cleaned, all_references, limit
        )
        if references := all_references[:limit]:
            result["references"] = {_SECTION: references}
        return result
