"""Regression tests for the EasyEDA/LCSC HTTP client."""

from __future__ import annotations

import unittest
from typing import Optional

from src.core import lcsc_api


class _Response:
    def __init__(self, status_code: int, payload: Optional[dict] = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.raise_called = False

    def raise_for_status(self) -> None:
        self.raise_called = True
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _Requests:
    class codes:
        ok = 200

    def __init__(self, response: _Response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class LcscApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_requests = lcsc_api.requests

    def tearDown(self) -> None:
        lcsc_api.requests = self.original_requests

    def test_easyeda_requests_use_complete_browser_headers(self) -> None:
        response = _Response(200, {"result": "ok"})
        request = _Requests(response)
        lcsc_api.requests = request

        result = lcsc_api.LCSC_API().easyeda_get_device("device-id")

        self.assertEqual(result, {"result": "ok"})
        self.assertTrue(response.raise_called)
        headers = request.calls[0][1]["headers"]
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("AppleWebKit", headers["User-Agent"])
        self.assertIn("Chrome/", headers["User-Agent"])
        self.assertIn("application/json", headers["Accept"])

    def test_http_error_is_raised_before_json_decode(self) -> None:
        response = _Response(403)
        lcsc_api.requests = _Requests(response)

        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            lcsc_api.LCSC_API().easyeda_get_device("device-id")

        self.assertTrue(response.raise_called)


if __name__ == "__main__":
    unittest.main()
