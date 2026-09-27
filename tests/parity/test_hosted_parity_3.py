"""Hosted parity, replay group 3 of 3 (see ``test_hosted_parity.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.parity.corpus import Scenario
from tests.parity.hosted import ParityServer, check_scenario_through_the_server, hosted_cases


@pytest.mark.parametrize(("scenario", "mode"), hosted_cases(2))
def test_scenario_matches_golden_through_the_server(
    scenario: Scenario, mode: str, server: ParityServer, tmp_path: Path
) -> None:
    check_scenario_through_the_server(scenario, mode, server, tmp_path)
