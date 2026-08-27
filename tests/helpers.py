from __future__ import annotations

from collections import deque

from postdesk.errors import ApiError


class FixtureTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def request(self, method, url, parameters, *, timeout):
        self.calls.append(
            {"method": method, "url": url, "parameters": parameters, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError(f"No fixture response left for {method} {url}")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def assert_consumed(self):
        assert not self.responses
