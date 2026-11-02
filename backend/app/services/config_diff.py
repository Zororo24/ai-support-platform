"""A human-readable difference between two agent configurations.

Used by the Versions tab and the publish dialog so an admin can see exactly what changes before it
goes live (and what a rollback undoes). Lists are matched by name, not position, so reordering an
action is not reported as a change.
"""

from typing import Any

# Setting groups shown as one section each, with friendly labels.
SIMPLE_SECTIONS: list[tuple[str, str]] = [
    ("instructions", "Instructions"),
    ("language", "Language"),
    ("knowledge", "Knowledge"),
    ("expose_attributes", "Customer details the AI can see"),
    ("handoff", "Handing over to a person"),
    ("limits", "Limits"),
    ("privacy", "Privacy"),
]
NAMED_SECTIONS: list[tuple[str, str, str]] = [
    ("actions", "Actions", "action"),
    ("workflows", "Processes", "process"),
]


def _change(label: str, kind: str, before: Any = None, after: Any = None) -> dict[str, Any]:
    return {"label": label, "kind": kind, "before": before, "after": after}


def _flatten(prefix: str, value: Any) -> dict[str, Any]:
    """Nested dict -> {"a.b": leaf}. Lists and scalars are leaves."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, inner in value.items():
            out.update(_flatten(f"{prefix}.{key}" if prefix else str(key), inner))
        return out
    return {prefix: value}


def _compare(base_label: str, before: Any, after: Any) -> list[dict[str, Any]]:
    if before == after:
        return []
    if not isinstance(before, dict) and not isinstance(after, dict):
        return [_change(base_label, "changed", before, after)]
    flat_before = _flatten("", before) if isinstance(before, dict) else {"": before}
    flat_after = _flatten("", after) if isinstance(after, dict) else {"": after}
    changes: list[dict[str, Any]] = []
    for key in sorted(set(flat_before) | set(flat_after)):
        left, right = flat_before.get(key), flat_after.get(key)
        if left == right:
            continue
        label = f"{base_label}: {key}" if key else base_label
        if key not in flat_before:
            changes.append(_change(label, "added", None, right))
        elif key not in flat_after:
            changes.append(_change(label, "removed", left, None))
        else:
            changes.append(_change(label, "changed", left, right))
    return changes


def diff_configs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []

    for key, title in SIMPLE_SECTIONS:
        changes = _compare(title, before.get(key), after.get(key))
        if changes:
            sections.append({"title": title, "changes": changes})

    for key, title, noun in NAMED_SECTIONS:
        old = {item["name"]: item for item in before.get(key) or []}
        new = {item["name"]: item for item in after.get(key) or []}
        changes: list[dict[str, Any]] = []
        for name in sorted(set(old) | set(new)):
            if name not in old:
                changes.append(_change(f"New {noun} “{name}”", "added", None, new[name].get("description")))
            elif name not in new:
                changes.append(
                    _change(f"Removed {noun} “{name}”", "removed", old[name].get("description"), None)
                )
            else:
                changes.extend(_compare(f"{noun.capitalize()} “{name}”", old[name], new[name]))
        if changes:
            sections.append({"title": title, "changes": changes})

    known = {k for k, _ in SIMPLE_SECTIONS} | {k for k, _, _ in NAMED_SECTIONS}
    other = [
        c
        for key in sorted((set(before) | set(after)) - known)
        for c in _compare(key, before.get(key), after.get(key))
    ]
    if other:
        sections.append({"title": "Other settings", "changes": other})

    return {"identical": not sections, "sections": sections}
