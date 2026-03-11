"""LLM client — supports Bankr LLM Gateway, Anthropic API, Claude Code CLI, or any OpenAI-compatible provider."""

import os
import time
import subprocess
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError, RateLimitError
from config import LLM_GATEWAY_URL, CLAUDE_CODE_MODEL_MAP

# Provider constants
PROVIDER_BANKR = "bankr"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_CUSTOM = "custom"
PROVIDER_CLAUDE_CODE = "claude_code"

# Known provider base URLs
PROVIDER_URLS = {
    PROVIDER_BANKR: LLM_GATEWAY_URL,
    PROVIDER_OPENAI: "https://api.openai.com/v1",
}


class CreditsExhaustedError(Exception):
    pass


class LLMClient:
    def __init__(self, api_key: str, model: str,
                 provider: str = PROVIDER_BANKR, base_url: str = None):
        self.model = model
        self.provider = provider
        self._anthropic_client = None
        self._openai_client = None

        if provider == PROVIDER_CLAUDE_CODE:
            # No API client needed — we spawn `claude` CLI as subprocess
            return

        if provider == PROVIDER_ANTHROPIC:
            # Use native Anthropic SDK
            try:
                import anthropic
                self._anthropic_client = anthropic.Anthropic(
                    api_key=api_key,
                    max_retries=0,
                    timeout=300.0,
                )
            except ImportError:
                raise RuntimeError(
                    "Anthropic SDK not installed. Run: pip install anthropic"
                )
        else:
            # OpenAI-compatible (Bankr, OpenAI, OpenRouter, custom)
            url = base_url or PROVIDER_URLS.get(provider, LLM_GATEWAY_URL)
            self._openai_client = OpenAI(
                api_key=api_key,
                base_url=url,
                max_retries=0,
                timeout=300.0,
            )

    def solve(self, system_prompt: str, user_prompt: str, max_tokens: int = 16384) -> str:
        """Send challenge to LLM and return raw response text."""
        if self.provider == PROVIDER_CLAUDE_CODE:
            return self._solve_claude_code(system_prompt, user_prompt)
        if self._anthropic_client:
            return self._solve_anthropic(system_prompt, user_prompt, max_tokens)
        return self._solve_openai(system_prompt, user_prompt, max_tokens)

    def _solve_claude_code(self, system_prompt: str, user_prompt: str) -> str:
        """Solve via Claude Code CLI subprocess (uses Claude Max subscription)."""
        cli_model = CLAUDE_CODE_MODEL_MAP.get(self.model, "sonnet")
        full_prompt = f"{system_prompt}\n\n{user_prompt}"

        # Build clean env: remove CLAUDECODE to avoid nested session blocking
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        for attempt in range(2):
            try:
                result = subprocess.run(
                    ["claude", "-p", "--model", cli_model, "--output-format", "text"],
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=env,
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    if "rate" in stderr.lower() or "429" in stderr:
                        time.sleep(30 * (attempt + 1))
                        continue
                    raise RuntimeError(f"Claude Code CLI failed (exit {result.returncode}): {stderr[:500]}")
                output = result.stdout.strip()
                if not output:
                    raise RuntimeError("Claude Code CLI returned empty output")
                return output
            except subprocess.TimeoutExpired:
                if attempt == 0:
                    continue
                raise RuntimeError("Claude Code CLI timed out after 5 minutes (twice)")
            except FileNotFoundError:
                raise RuntimeError(
                    "Claude Code CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
                )

        raise RuntimeError("Claude Code CLI solve failed after retries")

    def _solve_anthropic(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        """Solve via native Anthropic API."""
        import anthropic

        last_err = None
        for attempt in range(3):
            try:
                response = self._anthropic_client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    temperature=0.2,
                )
                return response.content[0].text.strip()
            except anthropic.RateLimitError as e:
                last_err = e
                time.sleep(30 * (attempt + 1))
            except anthropic.APIStatusError as e:
                if e.status_code in (401, 403):
                    raise
                if e.status_code == 402 or "credit" in str(e).lower():
                    raise CreditsExhaustedError(str(e))
                if e.status_code >= 500:
                    last_err = e
                    time.sleep(30)
                else:
                    raise
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                last_err = e
                time.sleep(15)

        raise last_err or Exception("LLM solve failed after retries")

    def _solve_openai(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        """Solve via OpenAI-compatible API."""
        last_err = None
        for attempt in range(3):
            try:
                response = self._openai_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                return response.choices[0].message.content.strip()
            except RateLimitError as e:
                last_err = e
                time.sleep(30 * (attempt + 1))
            except APIStatusError as e:
                if e.status_code == 402:
                    raise CreditsExhaustedError(str(e))
                if e.status_code in (401, 403):
                    raise
                if e.status_code >= 500:
                    last_err = e
                    time.sleep(30)
                else:
                    raise
            except (APIConnectionError, APITimeoutError) as e:
                last_err = e
                time.sleep(15)
            except Exception as e:
                err_str = str(e)
                if "402" in err_str or "credits" in err_str.lower() or "billing" in err_str.lower():
                    raise CreditsExhaustedError(err_str)
                raise

        raise last_err or Exception("LLM solve failed after retries")

    def check_available(self) -> bool:
        """Quick check that the provider is reachable."""
        try:
            if self.provider == PROVIDER_CLAUDE_CODE:
                env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
                result = subprocess.run(
                    ["claude", "--version"],
                    capture_output=True, text=True, timeout=10, env=env,
                )
                return result.returncode == 0
            if self._anthropic_client:
                self._anthropic_client.models.list()
            elif self._openai_client:
                self._openai_client.models.list()
            return True
        except Exception:
            return False
