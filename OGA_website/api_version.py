"""``OGA-API-Version`` on every ``/api/`` reply.

So a client notices a new version on a request it was making anyway, and can
ask ``/api/v1/changelog/?since=<the version it knew>`` what changed — the API
telling its callers what moved, rather than API.md waiting to be read
(`core/api_changelog.py`). Exposed to browsers, because the data portal and
anything else calling from a page reads it with ``fetch``.
"""
HEADER = "OGA-API-Version"


class ApiVersionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/"):
            from core.api_changelog import CURRENT
            response[HEADER] = CURRENT
            exposed = response.get("Access-Control-Expose-Headers", "")
            response["Access-Control-Expose-Headers"] = (
                f"{exposed}, {HEADER}" if exposed else HEADER)
        return response
