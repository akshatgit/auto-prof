"""Ollama Cloud backend -- HTTP calls to the hosted Ollama API.

Uses stdlib `urllib.request` only (no `requests` dependency, matching the
"no heavy dependencies" design principle). The HTTP transport is
injectable via `http_call` so tests never touch the network.
"""

import json
import os
import urllib.error
import urllib.request

from .base import Backend, BackendResult


class OllamaCloudBackend(Backend):
    name = "ollama_cloud"

    DEFAULT_HOST = "https://ollama.com"
    DEFAULT_MODEL = "gpt-oss:120b"

    # 280s was a bare constructor default nothing could configure, and it
    # became a hard ceiling the moment generation moved to a slower, more
    # capable model. The longest jobs in the system -- revising a paper
    # against three full reviews, working a task with a large memory --
    # exceeded it EVERY attempt, so the retry policy burned all five tries
    # against the same wall and stranded the task. Retries cannot help
    # when the limit is fixed and the work is simply longer than it.
    #
    # 900s sits well inside the job leases (1800s for review, 3600s for
    # student work), so a slow call still finishes before another worker
    # can reclaim the job.
    DEFAULT_TIMEOUT_SECONDS = 900

    def __init__(self, model=None, api_key=None, host=None, timeout=None, http_call=None):
        self.model = model or self.DEFAULT_MODEL
        self.api_key = api_key if api_key is not None else os.environ.get("OLLAMA_API_KEY")
        self.host = (host or self.DEFAULT_HOST).rstrip("/")
        if timeout is None:
            configured = os.environ.get("AUTOPROF_OLLAMA_TIMEOUT")
            try:
                timeout = float(configured) if configured else self.DEFAULT_TIMEOUT_SECONDS
            except ValueError:
                timeout = self.DEFAULT_TIMEOUT_SECONDS
        self.timeout = timeout
        self.http_call = http_call or self._real_http_call

    def run(self, prompt: str, **opts) -> BackendResult:
        if not self.api_key:
            return BackendResult(text="", error="OLLAMA_API_KEY is not set")

        model = opts.get("model", self.model)
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.host}/api/generate"

        try:
            status, resp_headers, resp_body = self.http_call(url, headers, body, self.timeout)
        except TimeoutError:
            return BackendResult(text="", error=f"ollama cloud request timed out after {self.timeout}s")
        except OSError as e:
            return BackendResult(text="", error=f"ollama cloud request failed: {e}")

        if status == 429:
            retry_after = None
            ra_header = (resp_headers or {}).get("Retry-After")
            if ra_header:
                try:
                    retry_after = float(ra_header)
                except ValueError:
                    pass
            return BackendResult(text="", rate_limited=True, retry_after_seconds=retry_after)

        if status >= 400:
            snippet = (resp_body or b"")[:500]
            if isinstance(snippet, bytes):
                snippet = snippet.decode("utf-8", errors="replace")
            return BackendResult(text="", error=f"ollama cloud returned HTTP {status}: {snippet}")

        try:
            parsed = json.loads(resp_body)
        except json.JSONDecodeError:
            snippet = resp_body[:500] if isinstance(resp_body, (bytes, str)) else resp_body
            return BackendResult(text="", error=f"ollama cloud returned non-JSON response: {snippet}")

        return BackendResult(
            text=parsed.get("response", ""),
            model_version=parsed.get("model", model),
            raw=parsed,
        )

    @staticmethod
    def _real_http_call(url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()
