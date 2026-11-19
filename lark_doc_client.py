"""Minimal Lark (Feishu) Docs client that reads documents with a user_access_token.

Authentication, in order of precedence (environment variables or a `.env` file):
    1. LARK_USER_ACCESS_TOKEN   a fixed token (starts with "u-"); never refreshed
    2. LARK_APP_ID + LARK_APP_SECRET and a prior `python lark_oauth.py login`;
       tokens are stored on disk and refreshed automatically
    LARK_DOMAIN                 optional, default https://open.larksuite.com
                                (use https://open.feishu.cn for Feishu China)

Required app scopes:
    docx:document:readonly  read docx content
    wiki:node:read          only needed to resolve /wiki/ links
    offline_access          needed for refresh tokens

CLI usage:
    python lark_doc_client.py <doc-url-or-id>               # plain text
    python lark_doc_client.py <doc-url-or-id> --blocks      # block tree as JSON
    python lark_doc_client.py <doc-url-or-id> --meta        # title / revision
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlparse

from lark_oauth import LarkAuthError, LarkOAuth, UserTokenProvider, load_dotenv

DEFAULT_DOMAIN = "https://open.larksuite.com"
_URL_KIND_RE = re.compile(r"/(docx|docs|wiki)/([A-Za-z0-9]+)")
_BARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,}$")


class LarkApiError(RuntimeError):
    """Raised when the Lark API returns an HTTP error or a non-zero `code`."""

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def parse_doc_reference(ref: str) -> tuple[str, str]:
    """Return (kind, token) where kind is 'docx' or 'wiki'.

    Accepts a full URL (…/docx/<token>, …/wiki/<token>) or a bare token,
    which is treated as a docx document_id.
    """
    ref = ref.strip()
    if _BARE_TOKEN_RE.match(ref):
        return "docx", ref
    match = _URL_KIND_RE.search(urlparse(ref).path)
    if not match:
        raise ValueError(f"Cannot find a docx/wiki token in: {ref!r}")
    kind, token = match.groups()
    if kind == "docs":
        raise ValueError(
            "Legacy /docs/ documents are not supported by the docx v1 API. "
            "Upgrade the document to a new-version doc or use the old doc API."
        )
    return kind, token


class TokenProvider(Protocol):
    def get(self, force_refresh: bool = False) -> str: ...


# Lark error codes meaning the user access token is invalid or expired.
_TOKEN_EXPIRED_CODES = {99991668, 99991677}


class LarkDocClient:
    """Pass either a fixed `user_access_token` or a `token_provider` (see lark_oauth.UserTokenProvider).

    With a provider, the client refreshes the token and retries once when Lark rejects it as expired.
    """

    def __init__(
        self,
        user_access_token: str | None = None,
        domain: str = DEFAULT_DOMAIN,
        timeout: float = 30.0,
        token_provider: TokenProvider | None = None,
    ):
        if not user_access_token and token_provider is None:
            raise ValueError("Provide user_access_token or token_provider")
        self._static_token = user_access_token
        self._provider = token_provider
        self._base = domain.rstrip("/") + "/open-apis"
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "LarkDocClient":
        """Use LARK_USER_ACCESS_TOKEN if set, otherwise the tokens stored by `lark_oauth.py login`."""
        load_dotenv()
        domain = os.environ.get("LARK_DOMAIN", DEFAULT_DOMAIN)
        token = os.environ.get("LARK_USER_ACCESS_TOKEN", "")
        if token:
            return cls(token, domain)
        try:
            provider = UserTokenProvider(LarkOAuth.from_env())
        except LarkAuthError as exc:
            raise SystemExit(f"{exc}\nSet LARK_APP_ID / LARK_APP_SECRET (e.g. in .env), or LARK_USER_ACCESS_TOKEN.")
        return cls(domain=domain, token_provider=provider)

    # -- low level ---------------------------------------------------------

    def _token(self, force_refresh: bool = False) -> str:
        if self._provider is not None:
            return self._provider.get(force_refresh=force_refresh)
        return self._static_token or ""

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return self._get_once(path, params, self._token())
        except LarkApiError as exc:
            if self._provider is None or exc.code not in _TOKEN_EXPIRED_CODES:
                raise
            return self._get_once(path, params, self._token(force_refresh=True))

    def _get_once(self, path: str, params: dict[str, Any] | None, token: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body)
                raise LarkApiError(
                    f"HTTP {exc.code}: {err.get('msg', body)}", err.get("code"), exc.code
                ) from None
            except json.JSONDecodeError:
                raise LarkApiError(f"HTTP {exc.code}: {body[:300]}", None, exc.code) from None
        except urllib.error.URLError as exc:
            raise LarkApiError(f"Network error: {exc.reason}") from exc

        if payload.get("code", 0) != 0:
            raise LarkApiError(payload.get("msg", "unknown error"), payload.get("code"))
        return payload.get("data", {})

    # -- API calls ---------------------------------------------------------

    def resolve_wiki_token(self, wiki_token: str) -> str:
        """Turn a /wiki/<token> into the underlying docx document_id (obj_token)."""
        data = self._get("/wiki/v2/spaces/get_node", {"token": wiki_token, "obj_type": "wiki"})
        node = data.get("node", {})
        if node.get("obj_type") != "docx":
            raise LarkApiError(
                f"Wiki node is of type {node.get('obj_type')!r}, only 'docx' is supported."
            )
        return node["obj_token"]

    def resolve_document_id(self, ref: str) -> str:
        kind, token = parse_doc_reference(ref)
        return self.resolve_wiki_token(token) if kind == "wiki" else token

    def get_metadata(self, document_id: str) -> dict[str, Any]:
        return self._get(f"/docx/v1/documents/{document_id}").get("document", {})

    def get_raw_content(self, document_id: str) -> str:
        return self._get(f"/docx/v1/documents/{document_id}/raw_content").get("content", "")

    def get_blocks(self, document_id: str, page_size: int = 500) -> list[dict[str, Any]]:
        """Return every block of the document, following pagination."""
        blocks: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            data = self._get(
                f"/docx/v1/documents/{document_id}/blocks",
                {"page_size": page_size, "page_token": page_token},
            )
            blocks.extend(data.get("items", []))
            if not data.get("has_more"):
                return blocks
            page_token = data.get("page_token")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a Lark docx document.")
    parser.add_argument("document", help="Document URL (docx or wiki) or document_id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--blocks", action="store_true", help="print the block list as JSON")
    mode.add_argument("--meta", action="store_true", help="print document metadata as JSON")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = LarkDocClient.from_env()
    try:
        document_id = client.resolve_document_id(args.document)
        if args.blocks:
            print(json.dumps(client.get_blocks(document_id), ensure_ascii=False, indent=2))
        elif args.meta:
            print(json.dumps(client.get_metadata(document_id), ensure_ascii=False, indent=2))
        else:
            print(client.get_raw_content(document_id))
    except (LarkApiError, LarkAuthError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
