"""Write structured meeting data into Memgraph via Bolt protocol."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from neo4j import GraphDatabase, Driver

from .extractor import Extracted

if TYPE_CHECKING:
    from .composio_gmail import GmailMessage


class MemgraphError(RuntimeError):
    pass


class MemgraphWriter:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self._host = host or os.getenv("MEMGRAPH_HOST", "localhost")
        self._port = int(port or os.getenv("MEMGRAPH_PORT", "7687"))
        user = user or os.getenv("MEMGRAPH_USER", "")
        password = password or os.getenv("MEMGRAPH_PASSWORD", "")
        uri = f"bolt://{self._host}:{self._port}"
        auth = (user, password) if user else None
        self._driver: Driver = GraphDatabase.driver(uri, auth=auth)

    def close(self) -> None:
        self._driver.close()

    def ping(self) -> str:
        """Validate connection. Returns host:port string on success."""
        with self._driver.session() as s:
            result = s.run("RETURN 1 AS ok")
            result.single()
        return f"{self._host}:{self._port}"

    def write_meeting(
        self,
        msg: "GmailMessage | None",
        ex: Extracted,
        note_path: str,
        message_id: str,
    ) -> str:
        """Upsert all meeting nodes and relationships. Returns message_id."""
        with self._driver.session() as session:
            session.execute_write(
                _upsert_all, ex=ex, note_path=note_path, message_id=message_id
            )
        return message_id


def _upsert_all(tx, *, ex: Extracted, note_path: str, message_id: str) -> None:
    # ── Meeting node ──────────────────────────────────────────────────────────
    tx.run(
        """
        MERGE (m:Meeting {message_id: $mid})
        SET m.title            = $title,
            m.kind             = $kind,
            m.platform         = $platform,
            m.date             = $date,
            m.start_time       = $start_time,
            m.end_time         = $end_time,
            m.duration_minutes = $duration_minutes,
            m.summary          = $summary,
            m.sentiment        = $sentiment,
            m.confidence       = $confidence,
            m.note_path        = $note_path
        """,
        mid=message_id,
        title=ex.title,
        kind=ex.kind,
        platform=ex.platform,
        date=ex.date,
        start_time=ex.start_time,
        end_time=ex.end_time,
        duration_minutes=ex.duration_minutes,
        summary=ex.summary,
        sentiment=ex.sentiment,
        confidence=ex.confidence,
        note_path=note_path,
    )

    # ── Attendees ─────────────────────────────────────────────────────────────
    for attendee in ex.attendees:
        email = (attendee.get("email") or "").strip().lower()
        name = (attendee.get("name") or "").strip()
        role = (attendee.get("role") or "attendee").strip()
        identifier = email or name
        if not identifier:
            continue
        tx.run(
            """
            MERGE (p:Person {email: $email})
            SET p.name = CASE WHEN $name <> '' THEN $name ELSE p.name END
            WITH p
            MATCH (m:Meeting {message_id: $mid})
            MERGE (p)-[:ATTENDED {role: $role}]->(m)
            """,
            email=identifier,
            name=name,
            mid=message_id,
            role=role,
        )

    # ── Topics ────────────────────────────────────────────────────────────────
    for topic in ex.topics:
        if not topic:
            continue
        tx.run(
            """
            MERGE (t:Topic {name: $name})
            WITH t
            MATCH (m:Meeting {message_id: $mid})
            MERGE (m)-[:DISCUSSED]->(t)
            """,
            name=topic,
            mid=message_id,
        )

    # ── Decisions ─────────────────────────────────────────────────────────────
    for decision in ex.decisions:
        if not decision:
            continue
        tx.run(
            """
            MATCH (m:Meeting {message_id: $mid})
            MERGE (d:Decision {text: $text, meeting_id: $mid})
            MERGE (m)-[:DECIDED]->(d)
            """,
            text=decision,
            mid=message_id,
        )

    # ── Action Items ──────────────────────────────────────────────────────────
    for ai in ex.action_items:
        task = (ai.get("task") or "").strip()
        if not task:
            continue
        owner = (ai.get("owner") or "").strip()
        tx.run(
            """
            MATCH (m:Meeting {message_id: $mid})
            MERGE (a:ActionItem {task: $task, meeting_id: $mid})
            SET a.owner    = $owner,
                a.due      = $due,
                a.priority = $priority,
                a.done     = $done
            MERGE (m)-[:PRODUCED]->(a)
            """,
            task=task,
            mid=message_id,
            owner=owner or None,
            due=ai.get("due"),
            priority=ai.get("priority", "low"),
            done=bool(ai.get("done", False)),
        )
        if owner:
            tx.run(
                """
                MERGE (p:Person {email: $owner})
                SET p.name = CASE WHEN p.name IS NULL THEN $owner ELSE p.name END
                WITH p
                MATCH (a:ActionItem {task: $task, meeting_id: $mid})
                MERGE (a)-[:ASSIGNED_TO]->(p)
                """,
                owner=owner,
                task=task,
                mid=message_id,
            )
