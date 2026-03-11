"""Coordinator API wrapper — auth, challenge, submit, stake, epoch."""

import time
import secrets
import httpx
from config import COORDINATOR_URL
from retry import with_retry, RetryExhausted


class AuthError(Exception):
    pass


class InsufficientBalanceError(Exception):
    pass


class CoordinatorClient:
    def __init__(self, miner: str):
        self.miner = miner
        self.client = httpx.Client(
            base_url=COORDINATOR_URL,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        self._token = None
        self._token_time = 0.0

    def clear_token(self):
        """Invalidate cached auth token (call on 401)."""
        self._token = None
        self._token_time = 0.0

    def _auth_headers(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def authenticate(self, bankr) -> str:
        """Complete 3-step auth handshake. Returns bearer token."""
        # Step 1: Get nonce
        nonce_resp = with_retry(lambda: self.client.post(
            "/v1/auth/nonce", json={"miner": self.miner}
        ))
        message = nonce_resp["message"]

        # Step 2: Sign via Bankr
        signature = bankr.sign_message(message)

        # Step 3: Verify
        verify_resp = with_retry(lambda: self.client.post(
            "/v1/auth/verify", json={
                "miner": self.miner,
                "message": message,
                "signature": signature,
            }
        ))
        token = verify_resp.get("token")
        if not token:
            raise AuthError("No token in verify response")

        self._token = token
        self._token_time = time.time()
        return token

    def ensure_auth(self, bankr):
        """Re-auth if token is stale (>10 min old) or missing."""
        age = time.time() - self._token_time
        if not self._token or age > 600:
            self.authenticate(bankr)

    def get_challenge(self, nonce: str = None) -> dict:
        """Request a new challenge."""
        if nonce is None:
            nonce = secrets.token_hex(16)
        resp = with_retry(lambda: self.client.get(
            f"/v1/challenge?miner={self.miner}&nonce={nonce}",
            headers=self._auth_headers(),
        ))
        resp["_nonce"] = nonce
        return resp

    def submit_answer(self, challenge_id: str, artifact: str, nonce: str) -> dict:
        """Submit solved artifact."""
        return with_retry(lambda: self.client.post(
            "/v1/submit",
            json={
                "miner": self.miner,
                "challengeId": challenge_id,
                "artifact": artifact,
                "nonce": nonce,
            },
            headers=self._auth_headers(),
        ))

    def get_epoch(self) -> dict:
        """Get current epoch info."""
        return with_retry(lambda: self.client.get("/v1/epoch"))

    def get_credits(self) -> dict:
        """Get miner's credited solves."""
        return with_retry(lambda: self.client.get(
            f"/v1/credits?miner={self.miner}"
        ))

    def get_stake_approve_calldata(self, amount_wei: str) -> dict:
        return with_retry(lambda: self.client.get(
            f"/v1/stake-approve-calldata?amount={amount_wei}"
        ))

    def get_stake_calldata(self, amount_wei: str) -> dict:
        return with_retry(lambda: self.client.get(
            f"/v1/stake-calldata?amount={amount_wei}"
        ))

    def get_claim_calldata(self, epochs: list) -> dict:
        epoch_str = ",".join(str(e) for e in epochs)
        return with_retry(lambda: self.client.get(
            f"/v1/claim-calldata?epochs={epoch_str}"
        ))

    def get_unstake_calldata(self) -> dict:
        return with_retry(lambda: self.client.get("/v1/unstake-calldata"))

    def get_withdraw_calldata(self) -> dict:
        return with_retry(lambda: self.client.get("/v1/withdraw-calldata"))

    def get_withdrawable_at(self, miner: str = None) -> float:
        """Read withdrawableAt(address) from mining contract. Selector 0x5a8c06ab.
        Returns unix timestamp, or 0 if no pending unstake."""
        addr = (miner or self.miner).lower().replace("0x", "")
        from config import MINING_CONTRACT
        calldata = f"0x5a8c06ab000000000000000000000000{addr}"
        try:
            resp = httpx.post(
                "https://mainnet.base.org",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": MINING_CONTRACT, "data": calldata}, "latest"],
                },
                timeout=10.0,
            )
            result = resp.json().get("result", "0x0")
            return int(result, 16)
        except Exception:
            return 0

    def get_token_info(self) -> dict:
        return with_retry(lambda: self.client.get("/v1/token"))

    def get_staked_amount(self, miner: str = None) -> float:
        """Read stakedAmount(address) from the mining contract via Base RPC.

        Returns staked amount in whole tokens (not wei), or -1 on error.
        """
        addr = (miner or self.miner).lower().replace("0x", "")
        from config import MINING_CONTRACT
        calldata = f"0xf9931855000000000000000000000000{addr}"
        try:
            resp = httpx.post(
                "https://mainnet.base.org",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": MINING_CONTRACT, "data": calldata}, "latest"],
                },
                timeout=10.0,
            )
            result = resp.json().get("result", "0x0")
            return int(result, 16) / 10**18
        except Exception:
            return -1

    def is_eligible(self, miner: str = None) -> bool:
        """Read isEligible(address) from the mining contract."""
        addr = (miner or self.miner).lower().replace("0x", "")
        from config import MINING_CONTRACT
        calldata = f"0x66e305fd000000000000000000000000{addr}"
        try:
            resp = httpx.post(
                "https://mainnet.base.org",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": MINING_CONTRACT, "data": calldata}, "latest"],
                },
                timeout=10.0,
            )
            result = resp.json().get("result", "0x0")
            return int(result, 16) == 1
        except Exception:
            return False
