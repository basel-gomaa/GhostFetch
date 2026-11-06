"""Browser-hardening headers on every HTTP response (security review M-05).

The console is a React app served from the same origin as the API, and the
same origin also serves two noVNC viewers and the HTML dashboards a run
produces. Without these headers the console could be framed by another site
(clickjacking), a response could be MIME-sniffed into something executable,
and an XSS would have no second line of defence.

  Content-Security-Policy   scripts only from this origin; no plugins; this
                            origin may frame itself (the viewers and the
                            dashboard are same-origin iframes) and nobody else
                            may frame it. Websockets only back to this host.
  X-Frame-Options           SAMEORIGIN, for browsers that predate frame-ancestors
  X-Content-Type-Options    nosniff
  Referrer-Policy           strict-origin-when-cross-origin
  Permissions-Policy        no camera, microphone or geolocation
  Strict-Transport-Security only on HTTPS responses (behind the documented
                            proxy that is every public response), one year

ONE PAGE NEEDS INLINE SCRIPT and gets a looser script-src: the operator page
(/ops, a deliberately dependency-free single file). It is served from this
origin's own files and renders no user content.

A ROUTE'S OWN HEADER WINS. The artifact route already sends a sandboxing CSP
for run-produced HTML, which is stricter for that content than anything here,
so this middleware only fills in headers a response did not set itself.

Pure ASGI rather than BaseHTTPMiddleware: it touches only the response-start
message, so streaming responses and websockets pass through untouched.
"""

from __future__ import annotations

import re

_INLINE_SCRIPT_PATHS = re.compile(r"^/ops/?$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


def _csp(host: str | None, inline_scripts: bool) -> str:
    # The websocket sources name THIS host explicitly: 'self' covers ws: in
    # current browsers but not in all, and a wildcard ws: would let injected
    # script open a socket to anywhere.
    sockets = f" ws://{host} wss://{host}" if host and _SAFE_HOST.match(host) else ""
    script = "'self' 'unsafe-inline'" if inline_scripts else "'self'"
    return "; ".join([
        "default-src 'self'",
        f"script-src {script}",
        # React sets inline style attributes; the console ships no inline CSS
        # blocks, but noVNC and /ops do.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        f"connect-src 'self'{sockets}",
        "frame-src 'self'",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])


class SecurityHeaders:
    def __init__(self, app, hsts_max_age: int = 31536000) -> None:
        self.app = app
        self.hsts = f"max-age={hsts_max_age}; includeSubDomains"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        https = scope.get("scheme") == "https"
        host = None
        for name, value in scope.get("headers") or []:
            if name == b"host":
                host = value.decode("latin-1")
                break
        inline = bool(_INLINE_SCRIPT_PATHS.match(path))

        async def send_with_headers(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}

                def add(name: str, value: str) -> None:
                    key = name.lower().encode("latin-1")
                    if key not in present:
                        headers.append((key, value.encode("latin-1")))
                        present.add(key)

                add("content-security-policy", _csp(host, inline))
                add("x-frame-options", "SAMEORIGIN")
                add("x-content-type-options", "nosniff")
                add("referrer-policy", "strict-origin-when-cross-origin")
                add("permissions-policy", "camera=(), microphone=(), geolocation=()")
                if https:
                    add("strict-transport-security", self.hsts)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
