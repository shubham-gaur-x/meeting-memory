"""Higher-level Jira push logic for meeting notes."""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extractor import Extracted
    from .jira_client import JiraClient

logger = logging.getLogger(__name__)

_JIRA_PRIORITY = {"high": "High", "medium": "Medium", "low": "Low"}


def push_meeting(
    client: "JiraClient",
    ex: "Extracted",
    note_path: str,
    message_id: str,
    *,
    project_key: str,
    issue_type: str = "Task",
    create_decisions: bool = False,
    decision_issue_type: str = "Task",
    dry_run: bool = False,
    board_id: str | None = None,
) -> list[dict]:
    """Create Jira issues for action items (and optionally decisions) in a meeting.

    Returns list of created issue dicts:
    [{"key": "PROJ-42", "url": "...", "kind": "action_item"|"decision",
      "summary": str, "action_item_index": int|None, "decision_index": int|None}]

    Returns [] immediately if action_items is empty and create_decisions is False,
    or if create_decisions is True but decisions is also empty.
    Dry-run: prints plan, returns list with key=None.
    """
    assignee_map = _load_assignee_map()

    # Fetch active sprint once upfront (only if board_id given and not dry-run)
    active_sprint_id: int | None = None
    if board_id and not dry_run:
        sprint = client.get_active_sprint(board_id)
        if sprint:
            active_sprint_id = sprint["id"]
            logger.info("Active sprint: %s (id=%s)", sprint.get("name"), active_sprint_id)

    items_to_create: list[dict] = []

    for i, item in enumerate(ex.action_items):
        items_to_create.append({
            "kind": "action_item",
            "index": i,
            "summary": item["task"],
            "owner": item.get("owner"),
            "due": item.get("due"),
            "priority": _get_priority(item),
            "issue_type": issue_type,
        })

    if create_decisions:
        for i, decision in enumerate(ex.decisions):
            items_to_create.append({
                "kind": "decision",
                "index": i,
                "summary": decision,
                "owner": None,
                "due": None,
                "priority": "medium",
                "issue_type": decision_issue_type,
            })

    if not items_to_create:
        return []

    results: list[dict] = []
    high_priority_keys: list[str] = []

    for item in items_to_create:
        assignee_id = resolve_assignee(client, item["owner"], assignee_map)
        description = _build_description(ex, item, note_path)
        priority = item["priority"]
        jira_priority = _JIRA_PRIORITY.get(priority, "Medium")

        labels = ["meeting-generated"] + list(ex.topics)
        if item["kind"] == "decision":
            labels.append("decision")

        fields: dict = {
            "project": {"key": project_key},
            "issuetype": {"name": item["issue_type"]},
            "summary": item["summary"][:255],
            "description": description,
            "labels": labels,
            "priority": {"name": jira_priority},
        }
        if item["due"]:
            fields["duedate"] = item["due"]
        if assignee_id:
            fields["assignee"] = {"accountId": assignee_id}

        if dry_run:
            sprint_hint = " → active sprint" if priority == "high" and board_id else " → backlog"
            print(
                f"[dry-run] Would create {item['kind']} [{priority}]{sprint_hint} in {project_key}: "
                f"{item['summary'][:80]!r}"
                + (f" (assignee: {assignee_id})" if assignee_id else " (unassigned)")
            )
            result_entry: dict = {
                "key": None,
                "url": None,
                "kind": item["kind"],
                "summary": item["summary"],
                "priority": priority,
            }
        else:
            resp = client.create_issue(fields)
            issue_key = resp["key"]
            domain = client.domain
            url = f"https://{domain}/browse/{issue_key}"
            logger.info("Created Jira issue %s [%s]: %s", issue_key, priority, item["summary"][:60])
            if priority == "high":
                high_priority_keys.append(issue_key)
            result_entry = {
                "key": issue_key,
                "url": url,
                "kind": item["kind"],
                "summary": item["summary"],
                "priority": priority,
            }

        if item["kind"] == "action_item":
            result_entry["action_item_index"] = item["index"]
            result_entry["decision_index"] = None
        else:
            result_entry["action_item_index"] = None
            result_entry["decision_index"] = item["index"]

        results.append(result_entry)

    # Move high-priority issues into the active sprint in one batch
    if high_priority_keys and active_sprint_id:
        try:
            client.add_to_sprint(active_sprint_id, high_priority_keys)
            logger.info("Added %d high-priority issue(s) to sprint %s: %s",
                        len(high_priority_keys), active_sprint_id, high_priority_keys)
        except Exception as exc:
            logger.warning("Could not add issues to sprint: %s", exc)

    return results


def _get_priority(item: dict) -> str:
    """Return priority from item field, falling back to due-date heuristic."""
    priority = item.get("priority")
    if priority in ("high", "medium", "low"):
        return priority
    due = item.get("due")
    if due:
        try:
            days = (date.fromisoformat(due) - date.today()).days
            if days <= 14:
                return "high"
            if days <= 60:
                return "medium"
        except ValueError:
            pass
    return "low"


def resolve_assignee(
    client: "JiraClient",
    owner_name: str | None,
    assignee_map: dict[str, str],
) -> str | None:
    """Resolve a meeting owner name to a Jira accountId.

    Resolution order:
    1. None input → return None
    2. Exact match in assignee_map (case-insensitive key)
    3. First-name-only match in assignee_map
    4. If JIRA_RESOLVE_USERS_BY_EMAIL=true AND owner looks like email → search_users()
    5. Return None + print dim warning
    """
    if not owner_name:
        return None

    owner_lower = owner_name.strip().lower()

    # Exact match (case-insensitive)
    for key, account_id in assignee_map.items():
        if key.lower() == owner_lower:
            return account_id

    # First-name match
    owner_first = owner_lower.split()[0] if owner_lower else ""
    for key, account_id in assignee_map.items():
        if key.lower().split()[0] == owner_first:
            return account_id

    # Email-based lookup (opt-in)
    if (
        os.getenv("JIRA_RESOLVE_USERS_BY_EMAIL", "false").lower() == "true"
        and "@" in owner_name
    ):
        try:
            users = client.search_users(owner_name)
            if users:
                return users[0]["accountId"]
        except Exception as exc:
            logger.debug("User search for %r failed: %s", owner_name, exc)

    print(f"  [jira] owner {owner_name!r} not in JIRA_ASSIGNEE_MAP — will be unassigned")
    return None


def _load_assignee_map() -> dict[str, str]:
    """Parse JIRA_ASSIGNEE_MAP env var (JSON). Returns {} on missing/malformed."""
    raw = os.getenv("JIRA_ASSIGNEE_MAP", "{}")
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    logger.warning("JIRA_ASSIGNEE_MAP is malformed JSON — falling back to empty map")
    return {}


# ---- ADF builder helpers ----------------------------------------------------


def _adf_doc(*nodes: dict) -> dict:
    return {"version": 1, "type": "doc", "content": list(nodes)}


def _adf_heading(text: str, level: int = 2) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_paragraph(*inline: dict) -> dict:
    return {"type": "paragraph", "content": list(inline)}


def _adf_text(text: str, bold: bool = False) -> dict:
    node: dict = {"type": "text", "text": text}
    if bold:
        node["marks"] = [{"type": "strong"}]
    return node


def _adf_bullet_list(items: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [_adf_paragraph(_adf_text(item))],
            }
            for item in items
        ],
    }


def _adf_rule() -> dict:
    return {"type": "rule"}


def _build_description(ex: "Extracted", item: dict, note_path: str) -> dict:
    """Build ADF description for a Jira issue."""
    vault_path = os.getenv("VAULT_PATH", "./vault")
    vault_name = os.path.basename(os.path.abspath(vault_path))

    # Derive note stem for Obsidian URI
    note_stem = ""
    if note_path:
        import pathlib
        note_stem = pathlib.Path(note_path).stem

    nodes: list[dict] = []

    # Meeting header
    nodes.append(_adf_heading(f"Meeting: {ex.title}", level=2))
    date_str = ex.date or "Unknown date"
    nodes.append(_adf_paragraph(
        _adf_text(f"Date: {date_str}  |  Platform: {ex.platform}")
    ))
    nodes.append(_adf_rule())

    # The specific item being tracked
    kind_label = "Action Item" if item["kind"] == "action_item" else "Decision"
    nodes.append(_adf_heading(kind_label, level=2))
    nodes.append(_adf_paragraph(_adf_text(item["summary"])))
    nodes.append(_adf_rule())

    # Context section
    nodes.append(_adf_heading("Context", level=2))
    if ex.summary:
        nodes.append(_adf_paragraph(_adf_text(f"Meeting summary: {ex.summary}")))

    if ex.decisions:
        nodes.append(_adf_paragraph(_adf_text("Decisions made:", bold=True)))
        nodes.append(_adf_bullet_list(ex.decisions))

    attendee_names = [a["name"] for a in ex.attendees if a.get("name")]
    if attendee_names:
        nodes.append(_adf_paragraph(_adf_text(f"Attendees: {', '.join(attendee_names)}")))

    nodes.append(_adf_rule())

    # Source section
    nodes.append(_adf_heading("Source", level=2))
    source_parts: list[dict] = []
    if note_stem:
        obsidian_uri = f"obsidian://open?vault={vault_name}&file=Meetings/{note_stem}"
        source_parts.append(_adf_text("View in Obsidian: "))
        source_parts.append({
            "type": "text",
            "text": obsidian_uri,
            "marks": [{"type": "link", "attrs": {"href": obsidian_uri}}],
        })
        nodes.append(_adf_paragraph(*source_parts))
    if note_path:
        nodes.append(_adf_paragraph(_adf_text(f"Path: {note_path}")))
    nodes.append(_adf_paragraph(_adf_text("Generated by meeting-memory pipeline")))

    return _adf_doc(*nodes)
