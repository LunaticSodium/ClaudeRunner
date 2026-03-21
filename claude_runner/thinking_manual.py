"""
claude_runner/thinking_manual.py

Supervisor Thinking Manual (§11) — two-track reasoning methodology.

Encodes human R&D methodology into LLM instructions:
  Track 1 (Creative) = Brainstorm — unconstrained adversarial questioning
  Track 2 (Controlled) = SWOT — systematic coverage of known failure categories
  Synthesis = Rank and pick the top issue (Track 1 wins ties)

The supervisor follows this playbook at scale. The design admits human
reasoning is better and makes the LLM mimic the process.

Hard cap: self_check_limit prevents Goodhart's Law (optimizing the metric
until it stops measuring what you care about).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import ClassVar, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single finding from either track."""

    track: Literal["track1", "track2"]
    category: str  # Track 1: "creative"; Track 2: specific category name
    description: str
    source: str = ""  # reasoning or literature reference
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    recommended_action: str = ""


@dataclass
class ThinkingResult:
    """Result of running the Thinking Manual."""

    stage: str
    findings: list[Finding] = field(default_factory=list)
    top_priority: Finding | None = None
    track1_ran: bool = False
    track2_ran: bool = False

    @property
    def has_findings(self) -> bool:
        return len(self.findings) > 0


# ---------------------------------------------------------------------------
# Thinking Manual
# ---------------------------------------------------------------------------


class ThinkingManual:
    """Two-track reasoning methodology for supervisor decisions.

    Track 1 (Creative) runs first — unconstrained adversarial questioning.
    Track 2 (Controlled) runs second — systematic checklist.
    Track 1 takes precedence when they disagree on priority.

    Application by stage:
      - Intake Validation (§8): Track 2 only (completeness, not discovery)
      - Feed-Forward Pre-Flight (§9): Track 1 + Track 2
      - Feedback Self-Check: Track 1 + Track 2
    """

    TRACK1_PROMPTS: ClassVar[list[str]] = [
        "What is the most important thing this mission assumes that has never been explicitly stated?",
        "If this run produces a plausible-looking but wrong result, what would cause that?",
        "What would a skeptical reviewer of these results immediately question?",
        "What does the mission not ask for that it probably should?",
    ]

    TRACK2_CATEGORIES: ClassVar[list[str]] = [
        "units_and_constants",
        "design_space_validity",
        "domain_grounding",
        "dependency_chain",
        "output_specification",
    ]

    TRACK2_DESCRIPTIONS: ClassVar[dict[str, str]] = {
        "units_and_constants": "Silent unit mismatches and default parameter values",
        "design_space_validity": "Does the search space reflect the actual problem domain",
        "domain_grounding": "Is there a numerical anchor to validate against",
        "dependency_chain": "Are upstream results valid for downstream assumptions",
        "output_specification": "Will the output actually answer the question being asked",
    }

    def build_prompt(
        self,
        context: str,
        stage: Literal["intake", "preflight", "self_check"],
    ) -> str:
        """Build the combined prompt for the appropriate tracks.

        Parameters
        ----------
        context:
            The project book content, mission description, and any relevant
            runtime state to reason about.
        stage:
            Which stage this is being run for — determines which tracks apply.

        Returns
        -------
        str
            The full prompt to send to the supervisor LLM.
        """
        sections: list[str] = [
            "You are the Supervisor applying the Thinking Manual (§11).",
            f"Stage: {stage}",
            "",
            "## Context",
            context,
            "",
        ]

        run_track1 = stage in ("preflight", "self_check")
        run_track2 = True  # Always runs

        if run_track1:
            sections.append("## Track 1 — Creative (run FIRST)")
            sections.append(
                "Unconstrained adversarial questioning. No reference to checklists. "
                "Goal: surface unknown unknowns before they are anchored by systematic thinking."
            )
            sections.append("Answer ALL of these prompts:")
            for i, prompt in enumerate(self.TRACK1_PROMPTS, 1):
                sections.append(f"  {i}. {prompt}")
            sections.append("")
            sections.append("You may reference web research or domain knowledge freely.")
            sections.append("Do NOT filter for expected answers.")
            sections.append("")

        if run_track2:
            sections.append("## Track 2 — Controlled (run SECOND)")
            sections.append(
                "Systematic coverage of known failure categories. "
                "Goal: ensure no standard failure mode is missed."
            )
            sections.append("Check ALL of these categories:")
            for cat in self.TRACK2_CATEGORIES:
                desc = self.TRACK2_DESCRIPTIONS[cat]
                sections.append(f"  - **{cat}**: {desc}")
            sections.append("")

        # Synthesis rule
        sections.append("## Synthesis Rule")
        sections.append(
            "After both tracks: identify the SINGLE highest-priority issue. "
            "If Track 1 and Track 2 disagree on priority, Track 1 takes precedence — "
            "unknown unknowns are more dangerous than known failure modes."
        )
        sections.append("")

        # Response format
        sections.append("## Response Format (JSON)")
        sections.append("```json")
        sections.append("{")
        sections.append('  "findings": [')
        sections.append("    {")
        sections.append('      "track": "track1" | "track2",')
        sections.append('      "category": "...",')
        sections.append('      "description": "...",')
        sections.append('      "source": "...",')
        sections.append('      "severity": "low" | "medium" | "high" | "critical",')
        sections.append('      "recommended_action": "..."')
        sections.append("    }")
        sections.append("  ],")
        sections.append('  "top_priority": {')
        sections.append('    "track": "...",')
        sections.append('    "description": "...",')
        sections.append('    "severity": "...",')
        sections.append('    "recommended_action": "..."')
        sections.append("  }")
        sections.append("}")
        sections.append("```")

        return "\n".join(sections)

    def parse_response(
        self,
        response_text: str,
        stage: str,
    ) -> ThinkingResult:
        """Parse the LLM's thinking manual response into structured findings.

        Gracefully handles malformed responses.
        """
        result = ThinkingResult(
            stage=stage,
            track1_ran=stage in ("preflight", "self_check"),
            track2_ran=True,
        )

        try:
            # Extract JSON from response
            if "```json" in response_text:
                start = response_text.index("```json") + 7
                end = response_text.index("```", start)
                json_str = response_text[start:end].strip()
            elif "{" in response_text:
                start = response_text.index("{")
                end = response_text.rindex("}") + 1
                json_str = response_text[start:end]
            else:
                logger.warning("ThinkingManual: no JSON found in response")
                return result

            data = json.loads(json_str)

            # Parse findings
            for f in data.get("findings", []):
                finding = Finding(
                    track=f.get("track", "track2"),
                    category=f.get("category", "unknown"),
                    description=f.get("description", ""),
                    source=f.get("source", ""),
                    severity=f.get("severity", "medium"),
                    recommended_action=f.get("recommended_action", ""),
                )
                result.findings.append(finding)

            # Parse top priority
            tp = data.get("top_priority")
            if tp:
                result.top_priority = Finding(
                    track=tp.get("track", "track1"),
                    category=tp.get("category", "synthesis"),
                    description=tp.get("description", ""),
                    source=tp.get("source", ""),
                    severity=tp.get("severity", "high"),
                    recommended_action=tp.get("recommended_action", ""),
                )

        except Exception as exc:  # noqa: BLE001
            logger.warning("ThinkingManual: failed to parse response: %s", exc)
            # Create a single finding from the raw text
            result.findings.append(Finding(
                track="track2",
                category="parse_error",
                description=response_text[:500] if response_text else "Empty response",
                severity="low",
                recommended_action="log — response could not be parsed",
            ))

        return result

    def format_for_audit(self, result: ThinkingResult) -> str:
        """Format a ThinkingResult for the audit log."""
        lines = [
            f"## Thinking Manual — {result.stage}",
            f"Track 1 ran: {result.track1_ran}",
            f"Track 2 ran: {result.track2_ran}",
            f"Findings: {len(result.findings)}",
        ]

        if result.top_priority:
            lines.append(f"\n### Top Priority [{result.top_priority.severity.upper()}]")
            lines.append(f"Track: {result.top_priority.track}")
            lines.append(f"Description: {result.top_priority.description}")
            lines.append(f"Action: {result.top_priority.recommended_action}")

        for i, f in enumerate(result.findings, 1):
            lines.append(f"\n### Finding {i} [{f.severity}] ({f.track})")
            lines.append(f"Category: {f.category}")
            lines.append(f"Description: {f.description}")
            if f.source:
                lines.append(f"Source: {f.source}")
            lines.append(f"Action: {f.recommended_action}")

        return "\n".join(lines)
