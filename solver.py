"""Prompt builder, artifact extraction, and local verification."""

import os
import re

# Load SKILL.md context at module level (once)
_SKILL_CONTEXT = ""
_skill_path = os.path.join(os.path.dirname(__file__), "SKILL.md")
if os.path.exists(_skill_path):
    with open(_skill_path, "r") as _f:
        _SKILL_CONTEXT = _f.read()


def build_prompt(challenge: dict) -> tuple:
    """Build system and user prompts from challenge data.

    Returns (system_prompt, user_prompt).
    """
    doc = challenge.get("doc", "")
    questions = challenge.get("questions", [])
    constraints = challenge.get("constraints", [])
    companies = challenge.get("companies", [])
    solve_instructions = challenge.get("solveInstructions", "")

    system_prompt = (
        "You are solving a constrained NLP challenge. Read the document carefully, "
        "answer the questions using ONLY factual data (ignore hypotheticals/speculation), "
        "then build a single-line artifact satisfying ALL constraints exactly. "
        "Answers must match a company name from the provided list exactly."
    )

    if _SKILL_CONTEXT:
        system_prompt += (
            "\n\n--- BOTCOIN MINING PROTOCOL REFERENCE ---\n"
            + _SKILL_CONTEXT
            + "\n--- END REFERENCE ---"
        )

    user_prompt = f"""DOCUMENT:
{doc}

COMPANIES (answers MUST be one of these exactly):
{', '.join(companies)}

QUESTIONS:
"""
    for i, q in enumerate(questions):
        user_prompt += f"Q{i+1}: {q}\n"

    user_prompt += "\nCONSTRAINTS (ALL must be satisfied):\n"
    for i, c in enumerate(constraints):
        user_prompt += f"C{i+1}: {c}\n"

    if solve_instructions:
        user_prompt += f"\nSOLVE INSTRUCTIONS:\n{solve_instructions}\n"

    user_prompt += """
INSTRUCTIONS:
1. For each company, extract: revenues (Q1-Q4, compute annual total), debt-to-equity, satisfaction score, employee count, CEO, HQ city/country, sector, founded year. Ignore hypothetical/speculative statements.
2. Answer each question with explicit calculations. Double-check against the companies list.
3. Parse each constraint: word count, acrostic pattern, forbidden letters, required equations (A+B=C), required names/values.
4. Build ONE line satisfying all constraints simultaneously.
5. VERIFY before outputting:
   - Count words by splitting on spaces — must match exactly
   - Check first letter of each relevant word for acrostic
   - Scan entire artifact for forbidden letter (case-insensitive)
   - Check equation arithmetic
   - Confirm required names are present and spelled correctly
6. If any check fails, fix and re-verify.

Output your final artifact inside <ARTIFACT> tags on its own line:
<ARTIFACT>your single-line artifact here</ARTIFACT>"""

    return system_prompt, user_prompt


def extract_artifact(raw_response: str) -> str:
    """Extract the artifact from LLM response — look for <ARTIFACT> tags first, then last line."""
    # Try to extract from <ARTIFACT> tags
    match = re.search(r'<ARTIFACT>(.*?)</ARTIFACT>', raw_response, re.DOTALL)
    if match:
        artifact = match.group(1).strip()
        # Remove any line breaks within the artifact
        artifact = ' '.join(artifact.split())
        return artifact

    # Fallback: take last non-empty line
    lines = [l.strip() for l in raw_response.strip().split("\n") if l.strip()]
    if not lines:
        return raw_response.strip()
    return lines[-1]


def verify_artifact(artifact: str, challenge: dict) -> tuple:
    """Local pre-verification of the artifact.

    Returns (all_passed: bool, issues: list[str]).
    """
    issues = []
    constraints = challenge.get("constraints", [])
    words = artifact.split(" ")

    for i, con in enumerate(constraints):
        cl = con.lower()

        # Word count check
        m = re.search(r'exactly\s+(\d+)\s+words', cl)
        if m:
            expected = int(m.group(1))
            if len(words) != expected:
                issues.append(f"C{i+1}: Word count {len(words)}, expected {expected}")

        # Forbidden letter check
        if "not contain the letter" in cl:
            try:
                letter = con.split('"')[1].lower()
                if letter in artifact.lower():
                    issues.append(f"C{i+1}: Forbidden letter '{letter}' found")
            except IndexError:
                pass

        # Acrostic check (partial — verify format)
        if "acrostic" in cl and "first" in cl and "letters" in cl:
            m2 = re.search(r'first\s+(\d+)\s+words', cl)
            if m2:
                n = int(m2.group(1))
                acrostic = "".join(w[0].upper() for w in words[:n] if w)
                issues.append(f"C{i+1}: Acrostic = {acrostic} (verify manually)")

        # Equation check
        if "equation" in cl and "a+b=c" in cl.replace(" ", "").lower():
            eq_match = re.search(r'\d+\+\d+=\d+', artifact)
            if not eq_match:
                issues.append(f"C{i+1}: No equation A+B=C found in artifact")

    return (len([i for i in issues if "verify manually" not in i]) == 0, issues)
