"""Tests for the TUI action wrappers (tui/actions.py).

The wrappers must delegate to the existing cli.cmd_* handlers (single source of
truth for guards/precheck/backend calls), passing the namespace fields each
handler reads. We patch the handlers and assert delegation + key arguments.
(Retry no longer goes through here — it calls the retry core directly; see
test_tui_app.TestDashboardActionFlows.)
"""

from unittest.mock import AsyncMock, patch

import pytest

from tui import actions


@pytest.mark.asyncio
async def test_do_clear_set_forces_yes():
    with patch("cli.cmd_clear_workspace", new=AsyncMock(return_value=0)) as m:
        await actions.do_clear_set(2)
    ns = m.await_args.args[0]
    assert ns.set == 2
    assert ns.yes is True  # TUI confirms separately; handler must not prompt
