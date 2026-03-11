"""Core mining loop state machine — thread-safe for web UI."""

import time
import secrets
from config import RATE_LIMIT_SECONDS, MAX_CONSECUTIVE_FAILS
from solver import build_prompt, extract_artifact, verify_artifact
from llm_client import CreditsExhaustedError
from retry import RetryExhausted, HTTPError


class MiningLoop:
    def __init__(self, coordinator, bankr, llm, credits_monitor, ui):
        self.coordinator = coordinator
        self.bankr = bankr
        self.llm = llm
        self.credits_monitor = credits_monitor
        self.ui = ui

    def run(self):
        """Run the mining loop indefinitely (called from background thread)."""
        state = self.ui.state

        while True:
            # Check if mining is paused via web UI
            if not state.mining_active:
                self.ui.set_phase("PAUSED")
                time.sleep(1)
                continue

            try:
                self._mine_one(state)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.ui.log(f"Unexpected error: {e}")
                state.consecutive_fails += 1
                state.bump()
                if state.consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    self.ui.set_phase("PAUSED")
                    self.ui.log(f"{MAX_CONSECUTIVE_FAILS} consecutive failures. Pausing 5 min.")
                    self.ui.log("Consider changing model or checking LLM credits.")
                    time.sleep(300)
                    state.consecutive_fails = 0

            # Cooldown
            self._cooldown(state)

    def _mine_one(self, state):
        """Execute one complete mining cycle."""

        # 1. Auth
        self.ui.set_phase("AUTHENTICATING")
        self.ui.log("Authenticating...")
        try:
            self.coordinator.ensure_auth(self.bankr)
        except Exception as e:
            self.ui.log(f"Auth failed: {e}")
            raise

        # 2. Check LLM credits
        credit_bal = self.credits_monitor.check_and_topup()
        if credit_bal >= 0:
            state.llm_credits = credit_bal
            self.ui.update()

        # 3. Request challenge
        self.ui.set_phase("REQUESTING")
        nonce = secrets.token_hex(16)
        self.ui.log(f"Requesting challenge (nonce: {nonce[:8]}...)")

        try:
            challenge = self.coordinator.get_challenge(nonce)
        except HTTPError as e:
            if e.status == 401:
                self.ui.log("Token expired, re-authenticating...")
                self.coordinator.clear_token()
                self.coordinator.authenticate(self.bankr)
                challenge = self.coordinator.get_challenge(nonce)
            elif e.status == 403:
                self.ui.log(f"403 — Not eligible to mine: {e.body[:300]}")
                self.ui.log(f"  Miner address: {self.coordinator.miner}")
                staked = self.coordinator.get_staked_amount()
                eligible = self.coordinator.is_eligible()
                self.ui.log(f"  On-chain stake: {staked:,.0f} BOTCOIN, eligible: {eligible}")
                self.ui.log("  If stake is correct, try re-authenticating or check coordinator status.")
                self.ui.log("Mining paused. Fix the issue, then click Start.")
                state.mining_active = False
                state.bump()
                return
            else:
                raise
        except RetryExhausted as e:
            self.ui.log(f"Challenge request failed after retries: {e}")
            raise

        challenge_id = challenge.get("challengeId", "unknown")
        epoch_id = challenge.get("epochId", 0)
        credits_per_solve = challenge.get("creditsPerSolve", 1)
        num_questions = len(challenge.get("questions", []))
        num_constraints = len(challenge.get("constraints", []))
        doc_len = len(challenge.get("doc", ""))

        state.current_challenge_id = challenge_id[:16] + "..."
        state.epoch_id = epoch_id
        state.challenge_questions = challenge.get("questions", [])
        state.challenge_constraints = challenge.get("constraints", [])
        state.challenge_doc_preview = challenge.get("doc", "")[:500]
        state.solve_artifact = ""
        state.solve_passed = ""
        state.solve_failed_constraints = []
        state.solve_verification_issues = []
        state.bump()
        self.ui.log(
            f"Challenge: {challenge_id[:16]}... | "
            f"Epoch {epoch_id} | {num_questions}Q | {num_constraints}C | "
            f"{doc_len} chars | {credits_per_solve} credit/solve"
        )

        # 4. Solve via LLM (use current model from state in case user changed it)
        current_model = state.model
        if current_model and current_model != self.llm.model:
            self.llm.model = current_model
            self.ui.log(f"Switched to model: {current_model}")

        self.ui.set_phase("SOLVING")
        self.ui.log(f"Solving with {self.llm.model}...")

        system_prompt, user_prompt = build_prompt(challenge)

        try:
            start_t = time.time()
            raw_response = self.llm.solve(system_prompt, user_prompt)
            solve_time = time.time() - start_t
        except CreditsExhaustedError:
            self.ui.log("LLM credits exhausted! Attempting top-up...")
            new_bal = self.credits_monitor.force_check()
            state.llm_credits = new_bal
            state.bump()
            if new_bal <= 0:
                self.ui.log("Top-up failed — no credits available. Top up at bankr.bot/llm")
                raise
            # Retry once after top-up
            self.ui.log(f"Credits now ${new_bal:.2f} — retrying solve...")
            start_t = time.time()
            raw_response = self.llm.solve(system_prompt, user_prompt)
            solve_time = time.time() - start_t

        artifact = extract_artifact(raw_response)
        state.llm_output = f"Solve time: {solve_time:.1f}s\n\n{raw_response[:2000]}"
        state.solve_artifact = artifact
        state.solve_time = solve_time
        state.bump()
        self.ui.log(f"Solved in {solve_time:.1f}s | Artifact: {artifact[:80]}...")

        # 5. Local verification
        self.ui.set_phase("VERIFYING")
        passed, issues = verify_artifact(artifact, challenge)
        state.solve_verification_issues = issues
        state.bump()
        for issue in issues:
            self.ui.log(f"  {issue}")

        # 6. Submit
        self.ui.set_phase("SUBMITTING")
        self.ui.log("Submitting answer...")

        try:
            result = self.coordinator.submit_answer(challenge_id, artifact, nonce)
        except HTTPError as e:
            if e.status == 401:
                self.coordinator.clear_token()
                self.coordinator.authenticate(self.bankr)
                result = self.coordinator.submit_answer(challenge_id, artifact, nonce)
            elif e.status == 404:
                self.ui.log("Stale challenge (404). Skipping.")
                state.total_fails += 1
                state.consecutive_fails += 1
                state.bump()
                return
            elif e.status == 403:
                self.ui.log("403 — Not eligible. Stake BOTCOIN and top up LLM credits.")
                state.mining_active = False
                state.bump()
                return
            else:
                raise
        except RetryExhausted as e:
            self.ui.log(f"Submit failed after retries: {e}")
            state.total_fails += 1
            state.consecutive_fails += 1
            state.bump()
            return

        if result.get("pass"):
            # SUCCESS
            self.ui.set_phase("POSTING_RECEIPT")
            state.total_solves += 1
            state.total_credits += credits_per_solve
            state.consecutive_fails = 0
            state.last_solve_time = time.time()
            state.solve_passed = "pass"
            state.solve_failed_constraints = []
            state.bump()
            self.ui.log(f"PASS! Credits earned: {credits_per_solve}")

            # 7. Post receipt on-chain
            tx = result.get("transaction")
            if tx:
                self.ui.log("Posting receipt on-chain...")
                try:
                    receipt = self.bankr.submit_transaction(tx, "Post BOTCOIN mining receipt")
                    tx_hash = receipt.get("transactionHash", "unknown")
                    self.ui.log(f"Receipt posted: {tx_hash[:16]}...")
                except Exception as e:
                    self.ui.log(f"Receipt failed: {e}")
            else:
                self.ui.log("No transaction in response")
        else:
            # FAILED
            failed = result.get("failedConstraintIndices", [])
            state.total_fails += 1
            state.consecutive_fails += 1
            state.solve_passed = "fail"
            state.solve_failed_constraints = failed
            state.bump()
            self.ui.log(f"FAIL — failed constraints: {failed}")

    def _cooldown(self, state):
        """Wait for rate limit cooldown with UI countdown."""
        self.ui.set_phase("COOLDOWN")
        for remaining in range(RATE_LIMIT_SECONDS, 0, -1):
            # Allow early exit if mining was stopped
            if not state.mining_active:
                state.cooldown_remaining = 0
                state.bump()
                return
            state.cooldown_remaining = remaining
            state.bump()
            time.sleep(1)
        state.cooldown_remaining = 0
        state.bump()
