"""Artificial Analysis API client."""

from __future__ import annotations

import os
from typing import Any

import httpx

AA_BASE_URL = "https://artificialanalysis.ai/api/v2"
DEFAULT_TIMEOUT = 30.0
MAX_PAGES = 50
# Legacy /api/v2/data/* endpoints retire 2026-11-04 23:59 UTC (410 Gone).
# https://artificialanalysis.ai/data-api/migrate-v2-data
LLM_ENDPOINT = "/language/models/free"


class AAError(Exception):
    """Base error for AA API operations."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AAAuthError(AAError):
    """Authentication error (401)."""


class AARateLimitError(AAError):
    """Rate limit exceeded (429)."""


class AAServerError(AAError):
    """Upstream server error (5xx)."""


class AARequestError(AAError):
    """Invalid request or unsupported upstream response."""


def _get_api_key() -> str:
    key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY", "").strip()
    if not key:
        raise AAAuthError(
            "ARTIFICIAL_ANALYSIS_API_KEY environment variable is not set. "
            "Get your key at https://artificialanalysis.ai/account"
        )
    return key


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "Accept": "application/json",
    }


def _check_response(resp: httpx.Response) -> None:
    def _response_message(default: str) -> str:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if isinstance(body, dict):
            detail = body.get("error") or body.get("message") or body
        else:
            detail = body.strip()[:500]
        return f"{default}: {detail}" if detail else default

    if resp.status_code == 401:
        raise AAAuthError(
            "Invalid or missing API key. Check ARTIFICIAL_ANALYSIS_API_KEY.", 401
        )
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "unknown")
        raise AARateLimitError(
            f"Rate limit exceeded. Retry-After: {retry}s. "
            f"Free API limit is 1000 requests/day.",
            429,
        )
    if resp.status_code >= 500:
        raise AAServerError(
            _response_message(
                f"Artificial Analysis server error (HTTP {resp.status_code}). "
                "Try again later."
            ),
            resp.status_code,
        )
    if resp.status_code >= 400:
        raise AARequestError(
            _response_message(f"Artificial Analysis request failed (HTTP {resp.status_code})"),
            resp.status_code,
        )
    resp.raise_for_status()


class AAClient:
    """Synchronous client for the Artificial Analysis free API."""

    def __init__(self, api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key or _get_api_key()
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{AA_BASE_URL}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=_headers(self.api_key), params=params)
        _check_response(resp)
        return resp.json()

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        url = f"{AA_BASE_URL}{path}"
        headers = _headers(self.api_key)
        headers["Content-Type"] = "application/json"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=json_body)
        _check_response(resp)
        return resp.json()

    def _get_all(self, path: str, params: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch every page of a paginated endpoint (``pagination.has_more``)."""
        rows: list[dict[str, Any]] = []
        first: dict[str, Any] = {}
        for page in range(1, MAX_PAGES + 1):
            data = self._get(path, {**(params or {}), "page": page})
            if page == 1:
                first = data
            rows.extend(r for r in data.get("data", []) if isinstance(r, dict))
            pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
            if not pagination.get("has_more"):
                return rows, first
        raise AARequestError(f"{path}: more than {MAX_PAGES} pages")

    # ── LLM endpoints ──────────────────────────────────────────────

    def get_llm_models(self) -> list[dict[str, Any]]:
        """Fetch all LLM models with evaluations, pricing, and speed data."""
        rows, _ = self._get_all(LLM_ENDPOINT)
        return [normalize_llm(r) for r in rows]

    # ── Media endpoints ─────────────────────────────────────────────

    MEDIA_ENDPOINTS = {
        "text-to-image": "/media/text-to-image/models/free",
        "image-editing": "/media/image-editing/models/free",
        "text-to-speech": "/media/text-to-speech/models/free",
        "text-to-video": "/media/text-to-video/models/free",
        "image-to-video": "/media/image-to-video/models/free",
    }

    def get_media_models(
        self, modality: str, include_categories: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch media model rankings for a given modality."""
        endpoint = self.MEDIA_ENDPOINTS.get(modality)
        if endpoint is None:
            valid = ", ".join(self.MEDIA_ENDPOINTS.keys())
            raise AAError(f"Unknown modality '{modality}'. Valid: {valid}")
        params = {}
        if include_categories:
            params["include_categories"] = "true"
        rows, _ = self._get_all(endpoint, params)
        return normalize_media(rows)

    # ── CritPt endpoint ─────────────────────────────────────────────

    def evaluate_critpt(
        self,
        submissions: list[dict[str, Any]],
        batch_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a complete CritPt batch for official evaluation."""
        payload = {
            "submissions": submissions,
            "batch_metadata": batch_metadata or {},
        }
        return self._post("/critpt/evaluate", payload)

    # ── Healthcheck ─────────────────────────────────────────────────

    def healthcheck(self) -> dict[str, Any]:
        """Connectivity + auth check; walks every page so model_count is the full total."""
        rows, first = self._get_all(LLM_ENDPOINT)
        pagination = first.get("pagination") if isinstance(first.get("pagination"), dict) else {}
        return {
            "ok": True,
            "model_count": len(rows),
            "total_pages": pagination.get("total_pages"),
            "tier": first.get("tier"),
            "prompt_options": first.get("prompt_options", {}),
        }


def normalize_llm(m: dict[str, Any]) -> dict[str, Any]:
    """Map a v2 free-tier record back to the legacy flat shape the tools expect.

    Speed/latency moved under ``performance``; ``price_1m_blended_3_to_1`` is no
    longer returned, so it is derived as (3*input + output) / 4.
    """
    out = dict(m)
    perf = m.get("performance")
    if isinstance(perf, dict):
        for key, value in perf.items():
            out.setdefault(key, value)
        out.setdefault("median_time_to_first_answer_token", perf.get("median_time_to_first_answer_token_seconds"))
    pricing = dict(m.get("pricing") or {})
    inp, outp = pricing.get("price_1m_input_tokens"), pricing.get("price_1m_output_tokens")
    if pricing.get("price_1m_blended_3_to_1") is None and isinstance(inp, (int, float)) and isinstance(outp, (int, float)):
        pricing["price_1m_blended_3_to_1"] = round((3 * inp + outp) / 4, 6)
    out["pricing"] = pricing
    return out


def normalize_media(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v2 media rows carry ``ci_95`` and no ``rank``; restore both (rank by Elo)."""
    out = []
    for m in rows:
        item = dict(m)
        if "ci95" not in item and item.get("ci_95") is not None:
            item["ci95"] = item["ci_95"]
        out.append(item)
    ranked = sorted(out, key=lambda r: -(r.get("elo") or 0))
    for index, item in enumerate(ranked, start=1):
        item.setdefault("rank", index)
    return out
