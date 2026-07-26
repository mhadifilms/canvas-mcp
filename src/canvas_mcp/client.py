"""HTTP layer for talking to Canvas the same way its own web front end does.

Canvas's REST API accepts two kinds of credential: a developer token, or the
session cookie a logged-in browser holds. This client is built around the second
one, because a lot of institutions turn off the token page entirely - which is
exactly the situation this project exists for.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlparse

import httpx

from .config import StoredSession, normalize_base_url
from .errors import (
    AmbiguousCourseError,
    AuthError,
    CanvasMCPError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    CONNECT_HINT,
)

# Look like the browser whose cookie we are carrying; some Canvas deployments sit
# behind WAFs that dislike bare python-httpx user agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 canvas-mcp/0.1"
)
# Canvas serves 64-bit ids; the string-ids Accept header keeps them exact.
ACCEPT = "application/json+canvas-string-ids, application/json"

LOGIN_PATH_RE = re.compile(r"/login(/|$)|/saml|/sso|/oauth2/auth", re.IGNORECASE)


@dataclass
class Credentials:
    base_url: str
    cookies: dict[str, str] = field(default_factory=dict)
    token: str | None = None
    source: str = "unknown"

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def to_stored(self, user: dict[str, Any] | None = None) -> StoredSession:
        return StoredSession(
            base_url=self.base_url,
            cookies=self.cookies,
            token=self.token,
            source=self.source,
            saved_at=time.time(),
            user=user or {},
        )


class CanvasClient:
    """Thin async wrapper: pagination, CSRF, error translation, a little caching."""

    def __init__(
        self,
        creds: Credentials,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not creds.base_url:
            raise AuthError("No Canvas URL configured.", hint=CONNECT_HINT)
        self.creds = creds
        self.base_url = normalize_base_url(creds.base_url)

        cookies = httpx.Cookies()
        for name, value in (creds.cookies or {}).items():
            # Scope every cookie to the Canvas host so an off-host redirect
            # (a school SSO page, say) never receives the session.
            cookies.set(name, value, domain=creds.host)

        headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT, "X-Requested-With": "XMLHttpRequest"}
        if creds.token:
            headers["Authorization"] = f"Bearer {creds.token}"

        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._profile: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "CanvasClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- core request ------------------------------------------------------- #

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> httpx.Response:
        method = method.upper()
        url = path if path.startswith("http") else path
        headers: dict[str, str] = {}

        if method != "GET":
            headers["X-CSRF-Token"] = await self._csrf_token()

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method, url, params=params, data=data, headers=headers or None
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise CanvasMCPError(
                        f"Could not reach {self.base_url}: {exc}. Check your internet connection "
                        "(and that you are on the campus VPN if your school requires one)."
                    ) from exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 403 and self._is_rate_limited(response):
                if attempt == attempts - 1:
                    raise RateLimitError(
                        "Canvas is rate limiting this account. Wait a minute and try again."
                    )
                await asyncio.sleep(2 ** (attempt + 1))
                continue

            if response.status_code in (502, 503, 504) and attempt < attempts - 1:
                await asyncio.sleep(2**attempt)
                continue

            self._raise_for_status(response, path)
            return response

        raise CanvasMCPError(f"Request to {path} failed: {last_error}")

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.headers.get("X-Rate-Limit-Remaining") == "0":
            return True
        try:
            return "rate limit" in response.text.lower()
        except Exception:
            return False

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code

        # An unauthenticated API call usually 401s, but some deployments bounce the
        # request to an HTML login page instead. Both mean "log in again".
        content_type = response.headers.get("Content-Type", "")
        looks_like_login = (
            "text/html" in content_type
            and (path.startswith("/api/") or "/api/v1/" in str(response.url))
        ) or LOGIN_PATH_RE.search(response.url.path or "")

        if status == 401 or looks_like_login:
            raise AuthError(
                f"Canvas rejected the saved session for {self.base_url} (it expired, or you "
                "logged out of the browser it came from).",
                hint=CONNECT_HINT,
            )
        if status == 403:
            raise PermissionError_(
                f"Canvas says this account is not allowed to see {path}. "
                "That usually means the instructor has restricted it."
            )
        if status == 404:
            raise NotFoundError(f"Canvas has nothing at {path} (404).")
        if status >= 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    errors = body.get("errors") or body.get("message")
                    detail = f": {errors}" if errors else ""
            except Exception:
                detail = f": {response.text[:200]}" if response.text else ""
            raise CanvasMCPError(f"Canvas returned {status} for {path}{detail}")

    async def _csrf_token(self) -> str:
        """Canvas rejects non-GET requests unless the CSRF cookie is echoed as a header."""
        raw = self._http.cookies.get("_csrf_token")
        if not raw:
            # Hitting any page makes Canvas mint one for this session.
            try:
                await self._http.get("/")
            except httpx.TransportError:
                pass
            raw = self._http.cookies.get("_csrf_token")
        if not raw:
            raise CanvasMCPError(
                "Canvas did not provide a CSRF token, so changes cannot be submitted. "
                "Reading your courses still works."
            )
        return unquote(raw)

    # -- convenience -------------------------------------------------------- #

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.request("GET", path, params=params)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CanvasMCPError(f"Canvas returned something that is not JSON for {path}") from exc

    async def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 500,
        page_size: int = 100,
    ) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", min(page_size, 100))
        results: list[Any] = []
        url: str | None = path
        query: dict[str, Any] | None = params

        while url and len(results) < limit:
            response = await self.request("GET", url, params=query)
            try:
                page = response.json()
            except ValueError as exc:
                raise CanvasMCPError(f"Canvas returned non-JSON while paging {path}") from exc
            if isinstance(page, dict):
                # A few endpoints wrap the list, e.g. {"quizzes": [...]}.
                page = next((v for v in page.values() if isinstance(v, list)), [])
            results.extend(page)
            next_link = response.links.get("next", {}).get("url")
            url, query = (next_link, None) if next_link else (None, None)

        return results[:limit]

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        response = await self.request("POST", path, data=data)
        return response.json() if response.content else None

    async def put(self, path: str, data: dict[str, Any]) -> Any:
        response = await self.request("PUT", path, data=data)
        return response.json() if response.content else None

    async def delete(self, path: str) -> Any:
        response = await self.request("DELETE", path)
        return response.json() if response.content else None

    # -- cached lookups ----------------------------------------------------- #

    async def _cached(self, key: str, ttl: float, factory) -> Any:
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        value = await factory()
        self._cache[key] = (time.monotonic(), value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    async def profile(self) -> dict[str, Any]:
        if self._profile is None:
            self._profile = await self.get_json("/api/v1/users/self") or {}
        return self._profile

    async def courses(self, *, include_concluded: bool = False) -> list[dict[str, Any]]:
        key = f"courses:{include_concluded}"

        async def fetch() -> list[dict[str, Any]]:
            params: dict[str, Any] = {
                "include[]": [
                    "total_scores",
                    "current_grading_period_scores",
                    "term",
                    "teachers",
                    "favorites",
                ],
            }
            if include_concluded:
                params["state[]"] = ["available", "completed"]
            else:
                params["enrollment_state"] = "active"
            courses = await self.paginate("/api/v1/courses", params, limit=200)
            # Restricted/unpublished courses come back as stubs without a name.
            return [c for c in courses if isinstance(c, dict) and c.get("id") and c.get("name")]

        return await self._cached(key, 300, fetch)

    async def resolve_course(self, reference: str | int) -> dict[str, Any]:
        """Accept a course id, a course code, or a chunk of the course name.

        Students say "bio 101", not "course 482913", so matching has to be forgiving.
        """
        ref = str(reference).strip()
        if not ref:
            raise CanvasMCPError("Which course? Give me a course name, code, or id.")

        # Look at this term's courses first; only widen to concluded ones if nothing hits.
        courses = await self.courses()
        match = self._match_course(courses, ref)
        if match is None:
            widened = await self.courses(include_concluded=True)
            if len(widened) != len(courses):
                courses = widened
                match = self._match_course(courses, ref)
        if match is not None:
            return match

        if ref.isdigit():
            # Could be a course the enrollment listing skipped; ask Canvas directly.
            try:
                data = await self.get_json(
                    f"/api/v1/courses/{ref}", {"include[]": ["term", "teachers"]}
                )
            except NotFoundError:
                data = None
            if isinstance(data, dict) and data.get("id"):
                return data
            raise NotFoundError(f"No course with id {ref}.")

        available = "\n".join(
            f"  - {c.get('name')} ({c.get('course_code')}) - id {c.get('id')}" for c in courses[:20]
        )
        raise NotFoundError(f'No course matching "{ref}". Your courses:\n{available}')

    @staticmethod
    def _match_course(courses: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
        """Return the single course a reference names, or None. Raises if genuinely ambiguous."""
        if ref.isdigit():
            return next((c for c in courses if str(c.get("id")) == ref), None)

        needle = _normalize(ref)
        exact = [c for c in courses if _normalize(c.get("course_code", "")) == needle]
        if len(exact) == 1:
            return exact[0]

        partial = [
            c
            for c in courses
            if needle in _normalize(c.get("name", "")) or needle in _normalize(c.get("course_code", ""))
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            # A name that matches this term and a past term should resolve to this term.
            active = [c for c in partial if _is_active(c)]
            if len(active) == 1:
                return active[0]
            options = "\n".join(
                f"  - {c.get('name')} ({c.get('course_code')}) - id {c.get('id')}" for c in partial[:10]
            )
            raise AmbiguousCourseError(f'"{ref}" matches several courses:\n{options}')
        return None


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _is_active(course: dict[str, Any]) -> bool:
    for enrollment in course.get("enrollments") or []:
        if enrollment.get("enrollment_state") == "active":
            return True
    return False


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
