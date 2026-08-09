# POST /api/plugins/memory_hardening/reset_breaker
#
# Force-closes the circuit breaker and clears all recorded events.
# Useful after a transient FAISS issue has been resolved manually.

from __future__ import annotations

import json
from helpers.api import ApiHandler, Request, Response

from usr.plugins.memory_hardening.helpers import circuit_breaker as cb


class ResetBreaker(ApiHandler):
    @staticmethod
    def get_methods():
        return ["POST", "GET"]

    async def process(self, request: Request, response: Response):
        try:
            cb.reset_instance()
            body = json.dumps({"ok": True, "message": "breaker reset"})
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)})
            response.status_code = 500
        response.set_body(body)
        return response
