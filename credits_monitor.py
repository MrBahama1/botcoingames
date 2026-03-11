"""LLM credit monitoring and auto top-up via Bankr LLM Gateway."""

import time
import subprocess
import re
import json
from config import LLM_CREDIT_CHECK_INTERVAL, LLM_CREDIT_THRESHOLD, LLM_TOPUP_AMOUNT


class CreditsMonitor:
    def __init__(self, bankr, threshold=LLM_CREDIT_THRESHOLD,
                 topup_amount=LLM_TOPUP_AMOUNT, ui=None):
        self.bankr = bankr
        self.threshold = threshold
        self.topup_amount = topup_amount
        self.ui = ui
        self._last_check = 0.0
        self._last_balance = -1.0
        self._auto_topup_configured = False
        self._auto_topup_enabled = False  # user must opt in

    def _log(self, msg: str):
        if self.ui:
            self.ui.log(msg)

    def ensure_auto_topup(self):
        """Enable the gateway's built-in auto top-up. Best-effort — runs once."""
        if self._auto_topup_configured:
            return
        try:
            result = subprocess.run(
                ["bankr", "llm", "credits", "auto",
                 "--enable",
                 "--amount", str(self.topup_amount),
                 "--threshold", str(self.threshold),
                 "--tokens", "USDC"],
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0 or "enable" in output.lower():
                self._auto_topup_configured = True
                self._auto_topup_enabled = True
                self._log(f"Auto top-up enabled: ${self.topup_amount} when < ${self.threshold}")
            else:
                self._log(f"Auto top-up config: {output[:120]}")
        except FileNotFoundError:
            self._log("bankr CLI not found — auto top-up not configured. Install: npm i -g @bankr/cli")
        except subprocess.TimeoutExpired:
            self._log("bankr CLI timed out configuring auto top-up")
        except Exception as e:
            self._log(f"Auto top-up config error: {e}")

    def get_balance(self) -> float:
        """Check LLM credit balance via bankr CLI."""
        try:
            result = subprocess.run(
                ["bankr", "llm", "credits"],
                capture_output=True, text=True, timeout=15,
            )
            output = result.stdout + result.stderr
            m = re.search(r'\$?([\d.]+)', output)
            if m:
                self._last_balance = float(m.group(1))
                return self._last_balance
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Fallback: Bankr REST API balance check
        try:
            resp = self.bankr.prompt_and_poll("what are my LLM credits?", timeout=30)
            m = re.search(r'\$?([\d.]+)', resp)
            if m:
                self._last_balance = float(m.group(1))
                return self._last_balance
        except Exception:
            pass

        return self._last_balance

    def check_and_topup(self) -> float:
        """Check credits and trigger top-up if below threshold.

        Returns current balance (or -1 if unknown).
        """
        now = time.time()
        if now - self._last_check < LLM_CREDIT_CHECK_INTERVAL and self._last_balance > self.threshold:
            return self._last_balance

        self._last_check = now
        balance = self.get_balance()

        if 0 <= balance < self.threshold:
            if self._auto_topup_enabled:
                self._log(f"LLM credits low (${balance:.2f}). Topping up...")
                success = self._do_topup()
                if success:
                    time.sleep(5)
                    balance = self.get_balance()
                    self._log(f"LLM credits after top-up: ${balance:.2f}")
                else:
                    self._log("Top-up failed. Top up manually at bankr.bot/llm")
            else:
                self._log(f"LLM credits low (${balance:.2f}). Top up at bankr.bot/llm or enable auto top-up.")

        return balance

    def _do_topup(self) -> bool:
        """Top up LLM credits. Returns True if command succeeded."""
        # Method 1: bankr CLI direct top-up
        try:
            result = subprocess.run(
                ["bankr", "llm", "credits", "add",
                 str(self.topup_amount), "--token", "USDC", "-y"],
                capture_output=True, text=True, timeout=90,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0:
                self._log(f"Topped up ${self.topup_amount} LLM credits")
                return True
            self._log(f"Top-up CLI: {output[:150]}")
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            self._log("Top-up CLI timed out")
        except Exception as e:
            self._log(f"Top-up error: {e}")

        # Method 2: Bankr REST API prompt
        try:
            resp = self.bankr.prompt_and_poll(
                f"add ${self.topup_amount} to my LLM credits using USDC",
                timeout=120,
            )
            if "success" in resp.lower() or "added" in resp.lower() or "credit" in resp.lower():
                self._log(f"Top-up via API: {resp[:100]}")
                return True
            self._log(f"Top-up response: {resp[:150]}")
        except Exception as e:
            self._log(f"Top-up API error: {e}")

        return False

    def force_check(self) -> float:
        """Force an immediate balance check regardless of interval."""
        self._last_check = 0
        return self.check_and_topup()
