"""Scripted answers for actions during scenario tests and replays.

A `MockSet` outlives a single customer message (a scenario has several), so it counts how many
times each action has been called and walks through the scenario's `sequence` accordingly.
"""

from typing import Any

from app.agents.scenario_schema import MockResponse, MockSpec


class MockSet:
    def __init__(self, specs: dict[str, MockSpec] | None = None) -> None:
        self._specs = dict(specs or {})
        self._calls: dict[str, int] = {}

    def next(self, action: str) -> MockResponse | None:
        """The scripted answer for the next call of `action`, or None to use its sample response."""
        spec = self._specs.get(action)
        if spec is None:
            return None
        index = self._calls.get(action, 0)
        self._calls[action] = index + 1
        if spec.sequence:
            return spec.sequence[min(index, len(spec.sequence) - 1)]
        return MockResponse(result=spec.result, error=spec.error)


def mockset_from_recorded(calls: dict[str, list[dict[str, Any]]]) -> MockSet:
    """Build a MockSet from what an action actually returned in a past run (name -> outputs in order)."""
    specs: dict[str, MockSpec] = {}
    for name, outputs in calls.items():
        responses = [
            MockResponse(result=o.get("data"), error=None)
            if o.get("ok")
            else MockResponse(error=str(o.get("error") or "the action failed")[:300])
            for o in outputs
        ]
        if responses:
            specs[name] = MockSpec(sequence=responses)
    return MockSet(specs)
