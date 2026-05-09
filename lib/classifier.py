"""Decide whether a Gmail message is meeting-related.

We use a fast rules layer first (sender domain, subject keywords, calendar
MIME parts) and only fall back to an LLM call for the ambiguous cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .composio_gmail import GmailMessage


# senders that are almost always meetings
MEETING_SENDERS = {
    "noreply-calendar-invitation@google.com",
    "calendar-notification@google.com",
    "gemini-notes@google.com",          # Google Gemini auto-generated meeting notes
    "no-reply@zoom.us",
    "no-reply@us05web.zoom.us",
    "noreply@zoom.us",
    "no-reply@meet.google.com",
    "no-reply@chime.aws",
    "noreply@webex.com",
    "noreply@teams.microsoft.com",
    "fathom@fathom.video",
    "noreply@fathom.video",
    "team@otter.ai",
    "noreply@otter.ai",
    "noreply@fireflies.ai",
    "no-reply@fireflies.ai",
    "noreply@read.ai",
    "calendly.com",
    "no-reply@calendly.com",
    "savvycal.com",
    "no-reply@savvycal.com",
    "x.ai",
    "scheduling@x.ai",
}

MEETING_DOMAINS = {
    "calendar-notification@google.com",
    "fathom.video",
    "otter.ai",
    "fireflies.ai",
    "read.ai",
    "calendly.com",
    "savvycal.com",
    "zoom.us",
    "webex.com",
    "teams.microsoft.com",
    "meet.google.com",
    "chime.aws",
}

SUBJECT_HINTS = [
    r"\binvitation\b",
    r"\bmeeting\b",
    r"\bcall\b",
    r"\bsync\b",
    r"\b1:1\b",
    r"\bone[- ]on[- ]one\b",
    r"\bstandup\b",
    r"\bstand-up\b",
    r"\bcatch[- ]up\b",
    r"\bcheck[- ]in\b",
    r"\bdiscussion\b",
    r"\bagenda\b",
    r"\binterview\b",
    r"\bdebrief\b",
    r"\bplanning\b",
    r"\bretro(spective)?\b",
    r"\breview\b",
    r"\bworkshop\b",
    r"\bdemo\b",
    r"\bkick[- ]?off\b",
    r"\bintro\b",
    r"\bchat\b",
    r"\bquick (call|chat|sync)\b",
    r"\baction items?\b",
    r"\bnotes?(\s+from|:)\b",           # "Notes from …" and "Notes: …" (Gemini notes)
    r"\brecap of\b",
    r"\b(transcript|recording) (of|from|for)\b",
    r"\b(zoom|teams|meet|webex|chime) (recording|meeting|invite)\b",
    r"\bwhen2meet\b",
    r"\bcalendly\b",
    r"\bschedul(e|ing) a\b",
    r"^canceled event\b",               # Google Calendar cancellation emails
]
SUBJECT_HINT_RE = re.compile("|".join(SUBJECT_HINTS), re.IGNORECASE)

BODY_HINTS_RE = re.compile(
    r"(meeting (link|recording|transcript)|join (the )?meeting|"
    r"google meet|zoom\.us/(j|my)/|teams\.microsoft\.com/l/meetup|"
    r"action items?|attendees?:|agenda|recap|"
    r"begin:vcalendar|method:request)",
    re.IGNORECASE,
)


@dataclass
class Classification:
    is_meeting: bool
    score: float
    reason: str
    kind: str  # invite | recording | recap | transcript | conversation | other


def classify(msg: GmailMessage) -> Classification:
    score = 0.0
    reasons: list[str] = []
    kind = "other"

    sender = (msg.sender_email or "").lower()
    sender_domain = sender.split("@")[-1] if "@" in sender else ""

    if sender in MEETING_SENDERS:
        score += 0.8
        reasons.append(f"sender {sender}")
    if sender_domain in MEETING_DOMAINS:
        score += 0.5
        reasons.append(f"domain {sender_domain}")

    if SUBJECT_HINT_RE.search(msg.subject or ""):
        score += 0.4
        reasons.append("subject hint")

    if BODY_HINTS_RE.search(msg.body_text or ""):
        score += 0.3
        reasons.append("body hint")

    # Calendar invites attach a text/calendar part
    if "begin:vcalendar" in (msg.body_text or "").lower():
        score += 0.5
        reasons.append("vcalendar")

    # Heuristic kind detection
    subj = (msg.subject or "").lower()
    body = (msg.body_text or "").lower()
    if "begin:vcalendar" in body or "invitation" in subj:
        kind = "invite"
    elif "canceled event" in subj:
        kind = "invite"
    elif "recording" in subj or "recording" in body[:500]:
        kind = "recording"
    elif "transcript" in subj or "transcript" in body[:1000]:
        kind = "transcript"
    elif sender in {"gemini-notes@google.com"} or "notes from" in body[:200]:
        kind = "transcript"
    elif "recap" in subj or "notes from" in subj or subj.startswith("notes:") or "action items" in body:
        kind = "recap"
    elif sender_domain in MEETING_DOMAINS:
        kind = "recap"
    elif score >= 0.4:
        kind = "conversation"

    return Classification(
        is_meeting=score >= 0.6,
        score=round(score, 2),
        reason=", ".join(reasons) or "no signals",
        kind=kind,
    )
