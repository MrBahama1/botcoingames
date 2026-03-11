"""Prompt builder, artifact extraction, and local verification."""

import re


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
        "You are an expert analyst solving a hybrid NLP challenge. "
        "You will read a document about fictional companies and answer questions, "
        "then produce a single-line artifact satisfying all constraints exactly. "
        "You must be extremely precise with word counts, acrostics, arithmetic, "
        "and forbidden characters."
    )

    user_prompt = f"""## DOCUMENT
{doc}

## COMPANIES (valid answer names)
{', '.join(companies)}

## QUESTIONS
"""
    for i, q in enumerate(questions):
        user_prompt += f"Q{i+1}: {q}\n"

    user_prompt += "\n## CONSTRAINTS\n"
    for i, c in enumerate(constraints):
        user_prompt += f"C{i+1}: {c}\n"

    if solve_instructions:
        user_prompt += f"\n## SOLVE INSTRUCTIONS\n{solve_instructions}\n"

    user_prompt += """
## APPROACH
1. Extract ALL company data: founding year, public/private, quarterly revenue + growth, D/E, satisfaction, employees, CEO, HQ city/country, sector.
2. Watch for abbreviation collisions (two companies sharing the same initials).
3. Distinguish counterfactual/hypothetical statements from actual data.
4. Answer each question methodically, showing which companies qualify.
5. Derive constraint values: acrostic letters, forbidden letter check, nextPrime, modular arithmetic equation, required names/cities/countries.
6. Build the artifact satisfying ALL constraints.
7. Self-verify: count words, check acrostic, check forbidden letter, verify equation arithmetic.

Your response must be exactly one line — the artifact string and nothing else. Do NOT output "Q1:", "Looking at", "Let me", "First", "Answer:", or any reasoning. Do NOT explain your process. Output ONLY the single-line artifact that satisfies all constraints. No preamble. No JSON. Just the artifact."""

    return system_prompt, user_prompt


def extract_artifact(raw_response: str) -> str:
    """Extract the artifact from LLM response — take last non-empty line."""
    lines = [l.strip() for l in raw_response.strip().split("\n") if l.strip()]
    if not lines:
        return raw_response.strip()
    # Prefer the last non-empty line (model sometimes prepends reasoning)
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
