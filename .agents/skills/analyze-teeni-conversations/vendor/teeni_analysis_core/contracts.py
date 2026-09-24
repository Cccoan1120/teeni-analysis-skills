from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AnalysisSummary:
    total_rows: int
    conversation_count: int
    user_count: int
    opening_exposures: int
    opened_exposures: int
    parse_failures: int
    empty_responses: int


@dataclass(frozen=True)
class OpeningResult:
    cid: str
    client_id: str
    opening_id: str
    source_time: str
    opening_text: str
    next_user_text: str
    opened: bool
    follow_label: str
    confidence: str
    reason: str
    understanding_signal: bool
    subsequent_turns: int
    reached_third_turn: bool
    reached_fifth_turn: bool
    length: int
    sentence_count: int
    question_count: int
    pre_question_length: int
    term_hits: list[str]
    question_type: str
    answer_burden: str
    difficulty_score: int
    difficulty: str


@dataclass(frozen=True)
class IssueResult:
    record_id: str
    cid: str
    client_id: str
    category: str
    severity: str
    status: str
    evidence: str
    reason: str
    source_time: str
    turn_index: int
    session_turns: int
    intention: str
    sub_intention: str
    confidence: str
    suggestion: str


@dataclass
class AnalysisResult:
    summary: AnalysisSummary
    openings: list[OpeningResult] = field(default_factory=list)
    quality_issues: list[IssueResult] = field(default_factory=list)
    safety_issues: list[IssueResult] = field(default_factory=list)
    normalized_rows: list[dict[str, Any]] = field(default_factory=list)
