"""Memory as infrastructure (protocol s7, s8, s11, s12, s49).

Memory here is not a text dump. Three things are enforced:

1. Kinds are distinct (episodic / semantic / procedural / user / project /
   decision) and so are epistemic classes (s11) - a hypothesis is never stored
   as if it were sourced knowledge.
2. Nothing is stored reflexively. `assess()` scores a candidate on the s8
   dimensions and returns a decision with its reasons; storage happens only
   when expected future benefit exceeds the cost and risk of retention.
3. Information that decays carries a review date, so staleness is detectable
   instead of silently believed (s49).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis import events
from jarvis.store import Store, new_id, utcnow

# What the memory is about.
KINDS = ("episodic", "semantic", "procedural", "user", "project", "decision")

# Where the content's authority comes from (protocol s11). These are not
# interchangeable and are never silently promoted.
SOURCE_CLASSES = (
    "raw_information",   # retrieved from the world, unvetted
    "sourced_knowledge", # supported by an identifiable source
    "system_knowledge",  # established by repeated validated operation
    "user_provided",     # stated by the user
    "hypothesis",        # unverified possibility
    "lesson",            # generalization extracted from experience
    "unknown",
)

# Retention decisions.
STORE = "store"
SKIP = "skip"
REFER_TO_USER = "refer_to_user"


class SensitiveContentError(ValueError):
    """Raised when a write would persist content the sensitivity scan flagged."""

# Half-life in days used to derive a review date from the decay score.
_MAX_REVIEW_DAYS = 730

_SENSITIVE_PATTERNS = (
    (r"\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|bearer)\b", "credential-like term"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key material"),
    (r"\bpassw(?:or)?d\s*[:=]", "password assignment"),
    (r"\b(?:\d[ -]?){13,19}\b", "possible payment card number"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "possible national identifier"),
)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on",
    "with", "that", "this", "as", "at", "by", "be", "are", "was", "were", "we",
    "i", "you", "not", "but", "from", "has", "have", "had", "will", "should",
}


@dataclass
class Memory:
    id: str
    kind: str
    title: str
    body: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    source_class: str = "unknown"
    confidence: float = 0.5
    project: str | None = None
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    review_after: str | None = None
    superseded_by: str | None = None
    archived: int = 0
    access_count: int = 0
    last_used_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_stale(self) -> bool:
        if not self.review_after:
            return False
        return datetime.now(timezone.utc) > datetime.fromisoformat(self.review_after)


@dataclass
class Assessment:
    """The result of applying memory governance to a candidate (s8)."""

    decision: str
    expected_value: float
    scores: dict[str, float]
    reasons: list[str]
    nearest_id: str | None = None
    supersede_candidate: str | None = None

    @property
    def should_store(self) -> bool:
        return self.decision == STORE


def _stem(word: str) -> str:
    """Crude suffix normalizer, not a linguistic stemmer.

    It exists because exact token matching made retrieval miss obvious hits:
    a query for "how do I run the tests" did not match a memory titled
    "Test runner". Folding plurals and a few verb endings fixes that class of
    miss. It will mangle irregular words; that is acceptable because both the
    query and the stored text pass through the same function, so matching stays
    consistent even where the stem is not a real word.
    """
    w = word
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes", "zes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    stripped_verb_suffix = True
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("er"):
        w = w[:-2]
    else:
        stripped_verb_suffix = False
    # Only collapse a double letter this function created (runn -> run,
    # stopp -> stop); a word that natively ends in one keeps it (class).
    if stripped_verb_suffix and len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    if len(w) > 2 and w.endswith("i"):
        w = w[:-1] + "y"  # verifi -> verify, keeps it aligned with verifies
    return w


def tokenize(text: str) -> set[str]:
    """Content tokens for overlap scoring: lowercased, stopped and stemmed."""
    words = re.findall(r"[a-z0-9][a-z0-9._-]*", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2}


def sensitivity(text: str) -> tuple[float, list[str]]:
    """Heuristic sensitivity scan.

    Deliberately conservative and deliberately dumb: it flags candidates for a
    human decision, it does not certify that content is safe to retain.
    """
    hits = [label for pattern, label in _SENSITIVE_PATTERNS if re.search(pattern, text, re.I)]
    return (min(1.0, 0.6 + 0.2 * (len(hits) - 1)) if hits else 0.0), hits



_NEGATIONS = {
    "not", "no", "never", "cannot", "can't", "isn't", "doesn't", "won't",
    "without", "removed", "unsupported",
}


def contradiction_reason(
    a_title: str, a_body: str, b_title: str, b_body: str
) -> str | None:
    """Whether two pieces of content look like they contradict each other.

    A lexical heuristic, not a truth engine: it catches same-subject pairs that
    differ in polarity or in a stated number. It lives here rather than in
    retrieval because both governance (should this be stored?) and retrieval
    (are these hits consistent?) need the same judgement.
    """
    a_tokens, b_tokens = tokenize(a_title), tokenize(b_title)
    if not a_tokens or not b_tokens:
        return None
    if len(a_tokens & b_tokens) / len(a_tokens | b_tokens) < 0.5:
        return None

    a_words = set(re.findall(r"[a-z']+", a_body.lower()))
    b_words = set(re.findall(r"[a-z']+", b_body.lower()))
    if bool(a_words & _NEGATIONS) != bool(b_words & _NEGATIONS):
        return "same subject, opposite polarity"

    a_nums = set(re.findall(r"\d+(?:\.\d+)?", a_body))
    b_nums = set(re.findall(r"\d+(?:\.\d+)?", b_body))
    if a_nums and b_nums and not (a_nums & b_nums):
        return f"same subject, different stated values ({sorted(a_nums)} vs {sorted(b_nums)})"

    # Deliberately stops here. A looser "the wording differs" rule flags two
    # phrasings of the same fact as a contradiction, which is worse than
    # missing one: false conflicts train the reader to ignore real ones.
    return None


def assess(
    store: Store,
    *,
    kind: str,
    title: str,
    body: str,
    confidence: float = 0.5,
    durability: float = 0.5,
    utility: float = 0.5,
    context_specificity: float = 0.3,
    decay: float = 0.3,
    project: str | None = None,
    threshold: float = 0.18,
) -> Assessment:
    """Decide whether a candidate memory is worth retaining.

    Scores are caller-supplied judgements except uniqueness and sensitivity,
    which are measured against the existing store and the content itself.
    """
    _validate_kind(kind)
    for name, value in (
        ("confidence", confidence),
        ("durability", durability),
        ("utility", utility),
        ("context_specificity", context_specificity),
        ("decay", decay),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be within 0.0..1.0, got {value}")

    text = f"{title}\n{body}"
    uniqueness, nearest, overlap = _uniqueness(store, text, kind, project)
    nearest_id = nearest.id if nearest else None
    sens, sens_hits = sensitivity(text)

    # A near neighbour that *disagrees* is not redundancy - it is the single
    # most valuable thing to store, because keeping only the older belief is
    # how a system silently picks a winner (protocol s50). The uniqueness
    # penalty is waived so the conflict can become visible.
    conflict = (
        contradiction_reason(nearest.title, nearest.body, title, body) if nearest else None
    )
    if conflict:
        uniqueness = max(uniqueness, 0.9)

    expected_value = (
        durability * utility * uniqueness * confidence * (1.0 - 0.5 * context_specificity)
    )

    reasons: list[str] = []
    supersede_candidate: str | None = None
    decision = STORE
    if conflict:
        reasons.append(
            f"contradicts existing memory {nearest_id} ({conflict}); storing it is what "
            "makes the disagreement visible instead of silently keeping the older belief"
        )
    if sens > 0:
        decision = REFER_TO_USER
        reasons.append(
            "possibly sensitive content (" + ", ".join(sens_hits) + "); retention is the user's call"
        )
    elif conflict:
        # The value threshold exists to prevent clutter, not to adjudicate
        # truth. Letting it drop the weaker side of a disagreement would be
        # silently choosing a winner, which s50 forbids: the correct response
        # to a contradiction is to keep both and lower confidence.
        decision = STORE
        if expected_value < threshold:
            reasons.append(
                f"expected value {expected_value:.2f} is below the {threshold:.2f} threshold, "
                "waived because dropping it would silently settle a contradiction"
            )
    elif expected_value < threshold:
        decision = SKIP
        reasons.append(
            f"expected value {expected_value:.2f} below threshold {threshold:.2f}"
        )
    if uniqueness < 0.35 and nearest_id and not conflict:
        if overlap >= 0.85:
            reasons.append(f"already known: {overlap:.0%} token overlap with {nearest_id}")
        else:
            # Overlapping but not identical, and not detectably contradictory:
            # most likely a revision of what is already stored. Say so rather
            # than dropping it silently - the caller can supersede instead.
            supersede_candidate = nearest_id
            reasons.append(
                f"overlaps existing memory {nearest_id} ({overlap:.0%}) without clearly "
                "contradicting it; if this supersedes that memory, use memory.supersede()"
            )
    if decay > 0.7:
        reasons.append("high decay: schedule an early review or this becomes false quietly")
    if not reasons:
        reasons.append("durable, useful, distinct and confident enough to be worth retrieving later")

    return Assessment(
        decision=decision,
        expected_value=round(expected_value, 4),
        scores={
            "durability": durability,
            "utility": utility,
            "uniqueness": round(uniqueness, 4),
            "confidence": confidence,
            "sensitivity": sens,
            "context_specificity": context_specificity,
            "decay": decay,
        },
        reasons=reasons,
        nearest_id=nearest_id,
        supersede_candidate=supersede_candidate,
    )


def remember(
    store: Store,
    *,
    kind: str,
    title: str,
    body: str,
    actor: str = "claude",
    payload: dict[str, Any] | None = None,
    source: str | None = None,
    source_class: str = "unknown",
    confidence: float = 0.5,
    project: str | None = None,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    decay: float = 0.3,
    review_after: str | None = None,
    assessment: Assessment | None = None,
    allow_sensitive: bool = False,
    session_id: str | None = None,
) -> Memory:
    """Write a memory. Callers should normally assess() first.

    `assessment`, when supplied, is recorded with the write so the reason a
    memory exists stays inspectable.

    The sensitivity scan runs here too, not only in assess(). Governance that
    the primary write path can skip is not governance: without this, a caller
    that never assessed could persist a credential to disk unremarked. Passing
    `allow_sensitive=True` is the deliberate override and is recorded as one.
    """
    _validate_kind(kind)
    if source_class not in SOURCE_CLASSES:
        raise ValueError(f"unknown source_class {source_class!r}; expected {SOURCE_CLASSES}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be within 0.0..1.0")
    if source_class == "sourced_knowledge" and not source:
        raise ValueError("sourced_knowledge requires an identifiable source (protocol s12)")
    if not title.strip() or not body.strip():
        raise ValueError("a memory needs both a title and a body")

    sens, sens_hits = sensitivity(f"{title}\n{body}")
    if sens > 0 and not allow_sensitive:
        events.record(
            store,
            "memory.write",
            actor,
            outcome="denied",
            permission="create",
            session_id=session_id,
            memory_kind=kind,
            reason="sensitivity scan flagged this content",
            flags=sens_hits,
        )
        raise SensitiveContentError(
            "refusing to store possibly sensitive content ("
            + ", ".join(sens_hits)
            + "); retention is the user's decision - pass allow_sensitive=True to override"
        )

    now = utcnow()
    if review_after is None:
        review_after = _review_date(decay)
    mem = Memory(
        id=new_id("mem"),
        kind=kind,
        title=title,
        body=body,
        payload=payload or {},
        source=source,
        source_class=source_class,
        confidence=confidence,
        project=project,
        entities=entities or [],
        tags=tags or [],
        review_after=review_after,
        created_at=now,
        updated_at=now,
    )
    with store.transaction():
        store.execute(
            """INSERT INTO memories (id, kind, title, body, payload, source, source_class,
               confidence, project, entities, tags, review_after, superseded_by, archived,
               access_count, last_used_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,0,NULL,?,?)""",
            (
                mem.id,
                mem.kind,
                mem.title,
                mem.body,
                json.dumps(mem.payload, default=str),
                mem.source,
                mem.source_class,
                mem.confidence,
                mem.project,
                json.dumps(mem.entities),
                json.dumps(mem.tags),
                mem.review_after,
                mem.created_at,
                mem.updated_at,
            ),
        )
        events.record(
            store,
            "memory.write",
            actor,
            target=mem.id,
            permission="create",
            session_id=session_id,
            memory_kind=kind,
            source_class=source_class,
            confidence=confidence,
            sensitive_override=bool(sens > 0 and allow_sensitive),
            expected_value=assessment.expected_value if assessment else None,
            reasons=assessment.reasons if assessment else None,
        )
    return mem


def get(store: Store, memory_id: str) -> Memory | None:
    row = store.one("SELECT * FROM memories WHERE id = ?", (memory_id,))
    return _from_row(row) if row else None


def supersede(
    store: Store, old_id: str, new_id_: str, *, actor: str = "claude", note: str = ""
) -> None:
    """Replace a memory with a newer one, preserving the old record.

    Provenance survives correction: the superseded memory is archived, never
    deleted, so 'what did we believe, and when' remains answerable.
    """
    if get(store, old_id) is None:
        raise KeyError(f"no such memory: {old_id}")
    if get(store, new_id_) is None:
        raise KeyError(f"no such memory: {new_id_}")
    with store.transaction():
        store.execute(
            "UPDATE memories SET superseded_by = ?, archived = 1, updated_at = ? WHERE id = ?",
            (new_id_, utcnow(), old_id),
        )
        events.record(
            store,
            "memory.supersede",
            actor,
            target=old_id,
            permission="modify",
            replacement=new_id_,
            note=note,
        )


def touch(store: Store, memory_id: str) -> None:
    """Record that a memory was actually used, for retrieval-quality metrics."""
    store.execute(
        "UPDATE memories SET access_count = access_count + 1, last_used_at = ? WHERE id = ?",
        (utcnow(), memory_id),
    )
    store.commit()


def stale(store: Store, limit: int = 50) -> list[Memory]:
    """Live memories whose review date has passed (protocol s49)."""
    rows = store.query(
        """SELECT * FROM memories WHERE archived = 0 AND review_after IS NOT NULL
           AND review_after < ? ORDER BY review_after ASC LIMIT ?""",
        (utcnow(), limit),
    )
    return [_from_row(r) for r in rows]


def all_live(store: Store, kind: str | None = None, project: str | None = None) -> list[Memory]:
    sql = "SELECT * FROM memories WHERE archived = 0"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY created_at DESC"
    return [_from_row(r) for r in store.query(sql, tuple(params))]


def _uniqueness(
    store: Store, text: str, kind: str, project: str | None
) -> tuple[float, "Memory | None", float]:
    """1.0 means nothing like it is stored; 0.0 means it is already known.

    Returns the nearest same-kind memory too, so the caller can ask whether the
    overlap is redundancy or disagreement.
    """
    tokens = tokenize(text)
    if not tokens:
        return 1.0, None, 0.0
    best_overlap = 0.0
    best: Memory | None = None
    for row in store.query("SELECT * FROM memories WHERE archived = 0 AND kind = ?", (kind,)):
        candidate = _from_row(row)
        other = tokenize(f"{candidate.title}\n{candidate.body}")
        if not other:
            continue
        overlap = len(tokens & other) / len(tokens | other)
        if overlap > best_overlap:
            best_overlap, best = overlap, candidate
    return max(0.0, 1.0 - best_overlap), best, best_overlap


def _review_date(decay: float) -> str | None:
    if decay <= 0.05:
        return None  # treated as durable; no scheduled review
    days = max(7, int(_MAX_REVIEW_DAYS * (1.0 - decay) ** 2))
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="microseconds")


def _validate_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown memory kind {kind!r}; expected one of {KINDS}")


def _from_row(row) -> Memory:
    d: dict[str, Any] = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["entities"] = json.loads(d["entities"])
    d["tags"] = json.loads(d["tags"])
    return Memory(**d)
