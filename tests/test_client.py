from __future__ import annotations

import httpx
import pytest

from aa_mcp.client import AAClient, AARequestError, _check_response


class RecordingClient(AAClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _post(self, path: str, json_body: dict) -> dict:
        self.calls.append((path, json_body))
        return {"accuracy": 0.75}


def test_evaluate_critpt_posts_expected_payload() -> None:
    client = RecordingClient()
    submissions = [
        {
            "problem_id": "Challenge_1_main",
            "generated_code": "def solution(): return 42",
            "model": "example-model",
            "generation_config": {"temperature": 0},
        }
    ]

    result = client.evaluate_critpt(submissions, {"run_id": "local"})

    assert result == {"accuracy": 0.75}
    assert client.calls == [
        (
            "/critpt/evaluate",
            {"submissions": submissions, "batch_metadata": {"run_id": "local"}},
        )
    ]


def test_check_response_raises_structured_request_error() -> None:
    request = httpx.Request("POST", "https://artificialanalysis.ai/api/v2/critpt/evaluate")
    response = httpx.Response(
        400,
        json={"error": "Invalid request body"},
        request=request,
    )

    with pytest.raises(AARequestError) as exc:
        _check_response(response)

    assert exc.value.status_code == 400
    assert "Invalid request body" in str(exc.value)


class PagedClient(AAClient):
    def __init__(self, pages: dict[str, list[dict]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, dict(params or {})))
        pages = self.pages[path]
        page = (params or {}).get("page", 1)
        return {"data": pages[page - 1], "pagination": {"page": page, "has_more": page < len(pages)}}


def test_llm_models_use_v2_free_endpoint_and_walk_pages() -> None:
    client = PagedClient({"/language/models/free": [
        [{"slug": "a", "pricing": {"price_1m_input_tokens": 1, "price_1m_output_tokens": 5},
          "performance": {"median_output_tokens_per_second": 50, "median_time_to_first_token_seconds": 2}}],
        [{"slug": "b"}],
    ]})
    models = client.get_llm_models()
    assert [m["slug"] for m in models] == ["a", "b"]
    assert all("/data/" not in path for path, _ in client.calls)
    assert [params["page"] for _, params in client.calls] == [1, 2]
    assert models[0]["median_output_tokens_per_second"] == 50
    assert models[0]["pricing"]["price_1m_blended_3_to_1"] == 2.0


def test_media_models_use_v2_free_endpoint_and_restore_rank() -> None:
    client = PagedClient({"/media/text-to-image/models/free": [[
        {"slug": "low", "elo": 1000, "ci_95": 9}, {"slug": "high", "elo": 1200, "ci_95": 10}]]})
    models = client.get_media_models("text-to-image")
    assert {m["slug"]: m["rank"] for m in models} == {"high": 1, "low": 2}
    assert models[0]["ci95"] == 9


def test_healthcheck_counts_models_across_all_pages() -> None:
    client = PagedClient({"/language/models/free": [[{"slug": "a"}, {"slug": "b"}], [{"slug": "c"}]]})
    assert client.healthcheck()["model_count"] == 3
