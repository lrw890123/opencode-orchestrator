from __future__ import annotations

import base64
from http.client import HTTPException
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class OpenCodeError(RuntimeError):
    """Raised when the OpenCode HTTP contract fails."""

    def __init__(
        self,
        message: str = "OpenCode request failed",
        *,
        code: str = "request_failed",
        status: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.path = path


class OpenCodeSelectionError(OpenCodeError):
    """Raised when a requested model or effort is unavailable."""


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        directory: Path,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
        event_timeout: float = 1800.0,
        allow_remote: bool = False,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"invalid OpenCode server URL: {base_url}")
        if not allow_remote and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"non-loopback OpenCode server requires explicit override: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.directory = Path(directory)
        self.username = username or "opencode"
        self.password = password
        self.timeout = timeout
        self.event_timeout = event_timeout
        self._openapi_paths: set[str] | None = None

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.password is not None:
            credential = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {credential}"
        return headers

    def _url(
        self,
        path: str,
        *,
        scoped: bool = False,
        query: dict[str, str | int] | None = None,
    ) -> str:
        parameters: dict[str, str | int] = dict(query or {})
        if scoped:
            parameters["directory"] = str(self.directory)
        suffix = f"?{urlencode(parameters)}" if parameters else ""
        return f"{self.base_url}{path}{suffix}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        scoped: bool = False,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            self._url(path, scoped=scoped, query=query),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if response.status == 204 or not payload:
                    return None
                return json.loads(payload)
        except HTTPError as error:
            error.close()
            raise OpenCodeError(
                "OpenCode HTTP request failed",
                code="http_error",
                status=error.code,
                path=path,
            ) from error
        except URLError as error:
            raise OpenCodeError(
                "OpenCode connection failed",
                code="connection_error",
                path=path,
            ) from error
        except HTTPException as error:
            raise OpenCodeError(
                "OpenCode connection failed",
                code="connection_error",
                path=path,
            ) from error

    def health(self) -> dict:
        return self._request("GET", "/global/health")

    def openapi_paths(self) -> set[str]:
        if self._openapi_paths is None:
            document = self._request("GET", "/doc")
            self._openapi_paths = set((document.get("paths") or {}).keys())
        return set(self._openapi_paths)

    def create_session(self, title: str) -> dict:
        return self._request("POST", "/session", scoped=True, body={"title": title})

    def validate_model_selection(self, provider_id: str, model_id: str, effort: str) -> dict:
        catalog = self._request("GET", "/provider", scoped=True)
        providers = {
            provider.get("id"): provider
            for provider in catalog.get("all", [])
            if isinstance(provider, dict) and provider.get("id")
        }
        provider = providers.get(provider_id)
        if provider is None:
            available = ", ".join(sorted(providers)) or "none"
            raise OpenCodeSelectionError(
                f"OpenCode provider '{provider_id}' is unavailable; available providers: {available}"
            )
        model = (provider.get("models") or {}).get(model_id)
        if model is None:
            available = ", ".join(sorted((provider.get("models") or {}).keys())) or "none"
            raise OpenCodeSelectionError(
                f"OpenCode model '{provider_id}/{model_id}' is unavailable; "
                f"available models for {provider_id}: {available}"
            )
        variants = list((model.get("variants") or {}).keys())
        if effort not in variants:
            available = ", ".join(variants) or "none"
            raise OpenCodeSelectionError(
                f"OpenCode effort '{effort}' is unsupported for {provider_id}/{model_id}; "
                f"available efforts: {available}"
            )
        return {
            "providerID": provider_id,
            "modelID": model_id,
            "variant": effort,
        }

    def prompt_async(
        self,
        session_id: str,
        text: str,
        agent: str = "build",
        model: dict[str, str] | None = None,
        variant: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "agent": agent,
            "parts": [{"type": "text", "text": text}],
        }
        if model is not None:
            body["model"] = model
        if variant is not None:
            body["variant"] = variant
        self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            scoped=True,
            body=body,
        )

    def messages(self, session_id: str, limit: int = 100) -> list[dict]:
        return self._request(
            "GET",
            f"/session/{session_id}/message",
            scoped=True,
            query={"limit": limit},
        )

    def _pending_for_session(
        self,
        session_id: str,
        *,
        v2_template: str,
        legacy_path: str,
    ) -> list[dict]:
        has_v2 = v2_template in self.openapi_paths()
        scoped_items: list[dict] = []
        if has_v2:
            path = v2_template.replace("{sessionID}", session_id)
            payload = self._request("GET", path)
            items = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(items, list) and any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or item["id"] != item["id"].strip()
                or not isinstance(item.get("sessionID"), str)
                or not item["sessionID"].strip()
                or item["sessionID"] != item["sessionID"].strip()
                or item["sessionID"] != session_id
                for item in items
            ):
                items = None
            if not isinstance(items, list):
                raise OpenCodeError(
                    f"OpenCode returned an invalid pending-input list for {session_id}"
                )
            scoped_items = items

        try:
            payload = self._request("GET", legacy_path, scoped=True)
        except OpenCodeError as error:
            if not has_v2 or error.status != 404 or error.path != legacy_path:
                raise
            legacy_items: list[dict] = []
        else:
            if not isinstance(payload, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or item["id"] != item["id"].strip()
                or not isinstance(item.get("sessionID"), str)
                or not item["sessionID"].strip()
                or item["sessionID"] != item["sessionID"].strip()
                for item in payload
            ):
                raise OpenCodeError(
                    f"OpenCode returned an invalid pending-input list for {session_id}"
                )
            legacy_items = [item for item in payload if item["sessionID"] == session_id]

        merged: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in [*scoped_items, *legacy_items]:
            key = (item.get("id"), item["sessionID"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def pending_permissions(self, session_id: str) -> list[dict]:
        return self._pending_for_session(
            session_id,
            v2_template="/api/session/{sessionID}/permission",
            legacy_path="/permission",
        )

    def pending_questions(self, session_id: str) -> list[dict]:
        return self._pending_for_session(
            session_id,
            v2_template="/api/session/{sessionID}/question",
            legacy_path="/question",
        )

    def session_diff(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/diff", scoped=True)

    def session_status(self, session_id: str) -> dict | None:
        statuses = self._request("GET", "/session/status", scoped=True)
        return statuses.get(session_id)

    def session(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}", scoped=True)

    def abort(self, session_id: str) -> bool:
        return bool(self._request("POST", f"/session/{session_id}/abort", scoped=True))

    def event_response(self):
        request = Request(
            self._url("/event", scoped=True),
            headers=self._headers("text/event-stream"),
            method="GET",
        )
        try:
            return urlopen(request, timeout=self.event_timeout)
        except HTTPError as error:
            error.close()
            raise OpenCodeError(
                "OpenCode event connection failed",
                code="http_error",
                status=error.code,
                path="/event",
            ) from error
        except URLError as error:
            raise OpenCodeError(
                "OpenCode event connection failed",
                code="connection_error",
                path="/event",
            ) from error

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        response: str,
    ) -> bool:
        if response not in {"once", "always", "reject"}:
            raise ValueError(f"invalid permission response: {response}")
        if "/permission/{requestID}/reply" in self.openapi_paths():
            path = f"/permission/{request_id}/reply"
            body = {"reply": response}
        else:
            path = f"/session/{session_id}/permissions/{request_id}"
            body = {"response": response}
        return bool(self._request("POST", path, scoped=True, body=body))

    def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        if "/question/{requestID}/reply" not in self.openapi_paths():
            raise OpenCodeError("OpenCode server does not expose the supported question reply API")
        return bool(
            self._request(
                "POST",
                f"/question/{request_id}/reply",
                scoped=True,
                body={"answers": answers},
            )
        )
