"""LLM credit monitoring and auto top-up via Bankr LLM Gateway."""

import time
import httpx
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

    def _get_api_key(self) -> str:
        """Extract API key from the BankrClient."""
        return self.bankr.client.headers.get("X-API-Key", "")

    def ensure_auto_topup(self):
        """Enable the gateway's built-in auto top-up. Best-effort — runs once."""
        if self._auto_topup_configured:
            return
        try:
            api_key = self._get_api_key()
            if not api_key:
                self._log("No API key — auto top-up not configured")
                return
            resp = httpx.post(
                "https://api.bankr.bot/llm/credits/auto-topup",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={
                    "enabled": True,
                    "amountUsd": self.topup_amount,
                    "thresholdUsd": self.threshold,
                    "sourceToken": "USDC",
                },
                timeout=30,
            )
            if resp.status_code < 400:
                self._auto_topup_configured = True
                self._auto_topup_enabled = True
                self._log(f"Auto top-up enabled: ${self.topup_amount} when < ${self.threshold}")
            else:
                self._log(f"Auto top-up config: {resp.text[:120]}")
        except Exception as e:
            self._log(f"Auto top-up config error: {e}")

    def get_balance(self) -> float:
        """Check LLM credit balance via Bankr LLM Gateway API."""
        api_key = self._get_api_key()
        if not api_key:
            return self._last_balance
        try:
            resp = httpx.get(
                "https://llm.bankr.bot/v1/credits",
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            if resp.status_code < 400:
                data = resp.json()
                bal = data.get("balanceUsd", -1)
                if isinstance(bal, (int, float)):
                    self._last_balance = float(bal)
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
        """Top up LLM credits via Bankr API. Returns True if succeeded."""
        api_key = self._get_api_key()
        if not api_key:
            self._log("No API key — cannot top up")
            return False
        try:
            resp = httpx.post(
                "https://api.bankr.bot/llm/credits/topup",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"amountUsd": self.topup_amount, "sourceToken": "USDC"},
                timeout=90,
            )
            if resp.status_code < 400:
                self._log(f"Topped up ${self.topup_amount} LLM credits")
                return True
            self._log(f"Top-up failed ({resp.status_code}): {resp.text[:150]}")
        except Exception as e:
            self._log(f"Top-up error: {e}")

        return False

    def force_check(self) -> float:
        """Force an immediate balance check regardless of interval."""
        self._last_check = 0
        return self.check_and_topup()
