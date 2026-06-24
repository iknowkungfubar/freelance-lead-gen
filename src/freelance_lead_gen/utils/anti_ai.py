"""Consolidated anti-AI detection patterns shared across the pipeline.

Single source of truth for banned phrases and AI marker patterns used by
the personalisation, verification, and content-editing subsystems.

Exporting both plain-string (:data:`_BANNED_PHRASES`) and compiled-regex
(:data:`BANNED_PHRASE_PATTERNS`) forms lets each consumer use whichever
is most natural for its detection logic.
"""

from __future__ import annotations as _annotations

import re

# ── Plain-string banned phrases ──────────────────────────────────────────────────

_BANNED_PHRASES: list[str] = [
    # Directly from ANTI_AI_PROMPT.
    "i hope this message finds you well",
    "i came across your project",
    "i am writing to express my interest",
    "i believe my skills would be a great fit",
    "i look forward to the possibility",
    "please let me know if you have any questions",
    "i am confident that i can deliver",
    "thank you for considering my application",
    "best regards",
    "kind regards",
    "warm regards",
    "i would love to join your team",
    "i am excited about the opportunity",
    "i have reviewed your requirements",
    # Additional banned patterns.
    "i am writing to apply",
    "i am submitting my proposal",
    "i am very interested",
    "i believe that my experience",
    "as per your requirements",
    "please find attached",
    "i would be a great asset",
    "i am eager to",
    "let's connect",
    "looking forward to hearing from you",
    "don't hesitate to reach out",
    "feel free to contact me",
]

# ── Compiled-regex banned phrase patterns ────────────────────────────────────────

BANNED_PHRASE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bi hope this message finds you well\b", re.IGNORECASE),
    re.compile(r"\bi came across your (project|listing|post)\b", re.IGNORECASE),
    re.compile(r"\bi am writing to express (my|an) interest\b", re.IGNORECASE),
    re.compile(r"\bi believe my skills would be a great fit\b", re.IGNORECASE),
    re.compile(r"\bi look forward to the possibility\b", re.IGNORECASE),
    re.compile(r"\bplease let me know if you have any questions\b", re.IGNORECASE),
    re.compile(r"\bi am confident (that )?i can deliver\b", re.IGNORECASE),
    re.compile(r"\bthank you for considering (my|the)\b", re.IGNORECASE),
    re.compile(r"\b(best|kind|warm) regards\b", re.IGNORECASE),
    re.compile(r"\bi would love to (join|be a part of)\b", re.IGNORECASE),
    re.compile(r"\bi am excited about (the|this) opportunity\b", re.IGNORECASE),
    re.compile(r"\bi have reviewed your requirements\b", re.IGNORECASE),
    re.compile(r"\bas per your (requirements|request|needs)\b", re.IGNORECASE),
    re.compile(r"\bfeel free to (reach out|contact me)\b", re.IGNORECASE),
    re.compile(r"\bdon't hesitate to\b", re.IGNORECASE),
    re.compile(r"\bi would be a great (asset|addition|fit)\b", re.IGNORECASE),
    re.compile(r"\b(i'm|i am) writing to apply\b", re.IGNORECASE),
    re.compile(r"\blooking forward to hearing from you\b", re.IGNORECASE),
]

# ── AI marker patterns ───────────────────────────────────────────────────────────

_AI_MARKER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(as an AI|as a language model|as an LLM)\b", re.IGNORECASE),
    re.compile(r"\bI cannot\b.*\b(AI|language model)\b", re.IGNORECASE),
    re.compile(r"\bI don't have (access|personal|emotions)\b", re.IGNORECASE),
    re.compile(r"\bmy knowledge cutoff\b", re.IGNORECASE),
    re.compile(r"\bI'm an AI\b", re.IGNORECASE),
    re.compile(r"\bI was trained\b", re.IGNORECASE),
    re.compile(r"\bI'm (just|only) an AI\b", re.IGNORECASE),
]
