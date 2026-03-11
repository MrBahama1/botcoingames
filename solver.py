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
        "You are an expert analyst solving a hybrid NLP challenge about fictional companies. "
        "You must reason carefully through each question and constraint, then produce a single-line artifact. "
        "Be extremely precise: exact word counts, correct acrostic letters, valid arithmetic, "
        "forbidden character avoidance, and correct company/person/city names from the document. "
        "Show your full reasoning, then output the final artifact inside <ARTIFACT> tags."
    )

    user_prompt = f"""## DOCUMENT
{doc}

## COMPANIES (valid answer names — answers MUST match one of these exactly)
{', '.join(companies)}

## QUESTIONS
"""
    for i, q in enumerate(questions):
        user_prompt += f"Q{i+1}: {q}\n"

    user_prompt += "\n## CONSTRAINTS (ALL must be satisfied)\n"
    for i, c in enumerate(constraints):
        user_prompt += f"C{i+1}: {c}\n"

    if solve_instructions:
        user_prompt += f"\n## SOLVE INSTRUCTIONS\n{solve_instructions}\n"

    user_prompt += """
## SOLVING METHODOLOGY (follow these steps carefully)

### Step 1: Data Extraction
For EVERY company in the document, extract into a structured table:
- Full name + any abbreviations/aliases used in the text
- Founded year, public/private status
- ALL quarterly revenues (Q1-Q4) and growth rates — compute total annual revenue
- Debt-to-equity ratio, customer satisfaction score
- Employee count, CEO name, HQ city and country, sector

### Step 2: Identify Red Herrings
- IGNORE hypothetical/counterfactual statements ("if they had...", "would have been...", "could potentially...")
- IGNORE speculative projections — only use stated factual data
- Watch for abbreviation collisions — two companies may share initials; use the FULL company name from the companies list

### Step 3: Answer Questions
For each question, show your work:
- List ALL qualifying companies with their relevant data
- Perform explicit calculations (sums, comparisons, rankings)
- Double-check your answer matches a name in the companies list EXACTLY

### Step 4: Parse Constraints
Analyze each constraint to determine what the artifact needs:
- **Word count**: Exactly N words (spaces separate words)
- **Acrostic**: First letters of first N words must spell a specific sequence
- **Forbidden letter**: The artifact must NOT contain a specific letter (case-insensitive)
- **Equation A+B=C**: Must include a valid arithmetic equation using specific numbers
- **nextPrime(N)**: Find the smallest prime > N
- **Modular arithmetic**: Compute N mod M correctly
- **Required content**: Specific names, cities, countries, or values must appear

### Step 5: Build the Artifact
Construct a single line that satisfies ALL constraints simultaneously.
- Start with the acrostic words (first letters must spell the required sequence)
- Embed required names, values, equations
- Avoid the forbidden letter in EVERY word
- Hit the exact word count (count by splitting on spaces)

### Step 6: Self-Verify (CRITICAL)
Before outputting, verify EACH constraint:
1. Count words by splitting on single spaces — must match exactly
2. Check acrostic by taking first letter of each of the first N words
3. Search for the forbidden letter (case-insensitive) — must NOT appear anywhere
4. Verify equation arithmetic: A + B must actually equal C
5. Confirm all required names/cities/values are present and spelled correctly

If ANY check fails, rebuild the artifact and verify again.

### Step 7: Output
After all verification passes, output the final artifact inside tags:

<ARTIFACT>your single-line artifact here</ARTIFACT>

The artifact must be exactly one line with no line breaks."""

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
