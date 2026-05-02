from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class ApiClient:
    base_url: str
    anon_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.anon_key}",
            "apikey": self.anon_key,
            "x-client-info": "brandsight-automated-extraction/1.0.0",
        }

    def get_batches(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/batches")
        data = payload.get("data", payload)
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected batches payload: {payload}")
        return data

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        for batch in self.get_batches():
            if str(batch.get("id")) == str(batch_id):
                return batch
        raise RuntimeError(f"Batch not found: {batch_id}")

    def get_prompts(self, batch_id: str, brand_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/prompts",
            json={
                "batch_id": batch_id,
                "brand_id": brand_id,
                "limit": limit,
            },
        )
        data = payload.get("data", {})
        prompts = data.get("prompts") if isinstance(data, dict) else None
        if not isinstance(prompts, list):
            raise RuntimeError(f"Unexpected prompts payload: {payload}")
        return [{**prompt, "batch_id": batch_id, "brand_id": brand_id} for prompt in prompts]

    def prompt_output_exists(self, prompt_id: str, brand_id: str, batch_id: str | None) -> bool:
        if not prompt_id or not brand_id or not batch_id:
            return False

        response = requests.get(
            f"{self.base_url}/prompt-outputs",
            params={
                "prompt_id": prompt_id,
                "brand_id": brand_id,
                "batch_id": batch_id,
                "limit": "1",
            },
            headers=self.headers,
            timeout=30,
        )
        if response.ok:
            return parse_exists_response(response.json())

        if response.status_code == 405:
            payload = self._request(
                "POST",
                "/prompt-outputs/exists",
                json={"prompt_id": prompt_id, "brand_id": brand_id, "batch_id": batch_id},
            )
            return parse_exists_response(payload)

        if response.status_code == 404:
            return False

        if response.status_code not in {404, 405}:
            raise RuntimeError(f"Duplicate check failed ({response.status_code}): {response.text}")

        return False

    def save_prompt_output(self, output: dict[str, Any], max_retries: int = 4) -> dict[str, Any] | None:
        for attempt in range(max_retries + 1):
            response = requests.post(
                f"{self.base_url}/prompt-outputs",
                headers=self.headers,
                json=output,
                timeout=60,
            )

            if response.status_code == 429 and attempt < max_retries:
                wait_seconds = retry_after_seconds(response) or min(60, 2**attempt)
                time.sleep(wait_seconds)
                continue

            if response.status_code >= 500 and attempt < max_retries:
                time.sleep(min(60, 2**attempt))
                continue

            if response.status_code >= 400:
                raise RuntimeError(f"Save failed ({response.status_code}): {response.text}")

            payload = response.json()
            if payload.get("success") is False:
                raise RuntimeError(f"Save failed: {payload.get('error') or payload}")
            return payload.get("data", payload)

        return None

    def get_prompt_output(self, output_id: int | str) -> dict[str, Any] | None:
        outputs = self.get_prompt_outputs(output_id=output_id, limit=1)
        return outputs[0] if outputs else None

    def get_prompt_outputs(
        self,
        *,
        output_id: int | str | None = None,
        batch_id: str | None = None,
        brand_id: str | None = None,
        prompt_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params = {"limit": str(limit)}
        if output_id:
            params["output_id"] = str(output_id)
        if batch_id:
            params["batch_id"] = batch_id
        if brand_id:
            params["brand_id"] = brand_id
        if prompt_id:
            params["prompt_id"] = prompt_id

        response = requests.get(
            f"{self.base_url}/prompt-outputs",
            params=params,
            headers=self.headers,
            timeout=60,
        )
        if response.status_code == 404:
            return []
        if not response.ok:
            raise RuntimeError(f"Get prompt outputs failed ({response.status_code}): {response.text}")

        return parse_outputs_response(response.json())

    def update_prompt_output(self, output: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any] | None:
        output_id = output.get("id") or output.get("output_id") or output.get("prompt_output_id")
        if output_id:
            for method in ("PATCH", "PUT"):
                response = requests.request(
                    method,
                    f"{self.base_url}/prompt-outputs/{output_id}",
                    headers=self.headers,
                    json=patch,
                    timeout=60,
                )
                if response.ok:
                    payload = response.json() if response.content else {}
                    return payload.get("data", payload)
                if response.status_code not in {404, 405}:
                    raise RuntimeError(f"Update prompt output failed ({response.status_code}): {response.text}")

        identifier_patch = {
            **patch,
            "id": output_id,
            "prompt_id": output.get("prompt_id"),
            "brand_id": output.get("brand_id"),
            "batch_id": output.get("batch_id"),
        }
        response = requests.patch(
            f"{self.base_url}/prompt-outputs",
            headers=self.headers,
            json=identifier_patch,
            timeout=60,
        )
        if response.ok:
            payload = response.json() if response.content else {}
            return payload.get("data", payload)
        if response.status_code in {404, 405}:
            raise RuntimeError(
                "Prompt output update endpoint is unavailable. Expected PATCH/PUT /prompt-outputs/{id} "
                "or PATCH /prompt-outputs."
            )
        raise RuntimeError(f"Update prompt output failed ({response.status_code}): {response.text}")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=60,
            **kwargs,
        )
        if not response.ok:
            raise RuntimeError(f"{method} {path} failed ({response.status_code}): {response.text}")
        return response.json()


def parse_exists_response(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("exists") is True:
        return True
    data = payload.get("data")
    if payload.get("success") and isinstance(data, dict) and data.get("exists") is True:
        return True
    if isinstance(data, list) and len(data) > 0:
        return True
    if isinstance(data, dict) and isinstance(data.get("outputs"), list) and data["outputs"]:
        return True
    if isinstance(payload.get("outputs"), list) and payload["outputs"]:
        return True
    return False


def parse_outputs_response(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        output = data.get("output")
        if isinstance(output, dict):
            output_id = data.get("output_id")
            if output_id is not None and not output.get("id"):
                output = {**output, "id": output_id, "output_id": output_id}
            return [output]
        for key in ("outputs", "prompt_outputs", "promptOutputs", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if data.get("id") or data.get("output_id") or data.get("prompt_id"):
            return [data]
    for key in ("outputs", "prompt_outputs", "promptOutputs", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def retry_after_seconds(response: requests.Response) -> int | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0, int(header))
    except ValueError:
        return None
