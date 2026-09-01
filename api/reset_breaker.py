# POST /api/plugins/memory_hardening/reset_breaker
#
# Force-closes the circuit breaker and clears all recorded events.
# Useful after a transient FAISS issue has been resolved manually.

from __future__ import annotations

from typing import Any, Dict
from helpers.api import ApiHandler, Request

from usr.plugins.memory_hardening.helpers import circuit_breaker as cb


class ResetBreaker(ApiHandler):
    @staticmethod
    def get_methods():
        return ["POST", "GET"]

    # v0.5.3 fix: process(input_data, request) returning a dict -- the old
    # (request, response) + response.set_body() form raised AttributeError
    # (500) on every call.
    async def process(self, input: Any, request: Request) -> Dict[str, Any]:
        try:
            cb.reset_instance()
            return {"ok": True, "message": "breaker reset"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
