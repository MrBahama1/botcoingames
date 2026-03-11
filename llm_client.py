"""LLM client — supports Bankr LLM Gateway, Anthropic API, or any OpenAI-compatible provider."""

import time
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError, RateLimitError
from config import LLM_GATEWAY_URL

# Provider constants
PROVIDER_BANKR = "bankr"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_CUSTOM = "custom"

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
        if self._anthropic_client:
            return self._solve_anthropic(system_prompt, user_prompt, max_tokens)
        return self._solve_openai(system_prompt, user_prompt, max_tokens)

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
            if self._anthropic_client:
                # Just try a minimal call
                self._anthropic_client.models.list()
            elif self._openai_client:
                self._openai_client.models.list()
            return True
        except Exception:
            return False
