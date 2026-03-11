"""Bankr API wrapper — sign, submit, prompt+poll, balances."""

import time
import json
import httpx
from config import BANKR_API_URL
from retry import with_retry


class BankrClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self.client = httpx.Client(
            base_url=BANKR_API_URL,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def __repr__(self):
        return "BankrClient(api_key=***)"

    def get_me(self) -> dict:
        """Resolve wallet info."""
        return with_retry(lambda: self.client.get("/agent/me"))

    def get_balances(self, chain: str = "base") -> dict:
        """Get wallet balances (synchronous endpoint)."""
        return with_retry(lambda: self.client.get(f"/agent/balances?chains={chain}"))

    def sign_message(self, message: str) -> str:
        """Sign a message via personal_sign. Returns signature."""
        resp = with_retry(lambda: self.client.post("/agent/sign", json={
            "signatureType": "personal_sign",
            "message": message,
        }))
        return resp["signature"]

    def submit_transaction(self, tx: dict, description: str) -> dict:
        """Submit a raw transaction on-chain. Synchronous with confirmation."""
        return with_retry(
            lambda: self.client.post("/agent/submit", json={
                "transaction": {
                    "to": tx["to"],
                    "chainId": tx["chainId"],
                    "value": tx.get("value", "0"),
                    "data": tx["data"],
                },
                "description": description,
                "waitForConfirmation": True,
            }),
            max_attempts=3,
        )

    def prompt_and_poll(self, prompt: str, timeout: int = 180) -> str:
        """Submit a natural language prompt and poll until complete."""
        resp = self.client.post("/agent/prompt", json={"prompt": prompt})
        if resp.status_code >= 400:
            return f"Error: {resp.text}"
        data = resp.json()
        job_id = data.get("jobId")
        if not job_id:
            return data.get("response", str(data))

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            result = self.client.get(f"/agent/job/{job_id}")
            if result.status_code >= 400:
                continue
            rdata = result.json()
            status = rdata.get("status", "")
            if status in ("completed", "failed", "cancelled"):
                return rdata.get("response", str(rdata))
        return "Timeout waiting for Bankr job"
