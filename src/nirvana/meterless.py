"""Meterless Runtime as an OpenAI-compatible provider (contract 0.90.0), and the advisor that
asks it for a rewrite.

The contract, as Meterless publishes it for apps that add the provider:

* default base URL ``http://127.0.0.1:8088/v1``; any pasted URL (with or without a scheme,
  ``/v1``, ``/v1/chat/completions``, ``/v1/models`` or ``/healthz``) normalises to
  ``<host>/v1`` and never ``/v1/v1``;
* a real bearer token on every call (the runtime writes it to ``<stateDir>/proxy-token``);
  the placeholder ``rt-local`` is refused;
* ``meterless-app: <id>`` on every request and ``meterless-scope: user`` on chat;
* model ids are routing modes (``meterless/auto``, ``fast``, ``deep``, ``cheap``, ``local``,
  ``fusion``, ``meterless/<class>``...), read from ``GET /v1/models``;
* ``GET /v1/models`` decides status: 401/403 unauthorized, 200 online, anything else offline;
* auto-detect tries ports 8088, 8090, 8787 and 11434 on 127.0.0.1, localhost and [::1];
* 180 s timeout, because a cold model load can take minutes.

The model never decides a verdict. It proposes SQL; the judge runs it and compares rows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

APP_ID = "nirvana"
DEFAULT_URL = "http://127.0.0.1:8088/v1"
PLACEHOLDER_TOKEN = "rt-local"
DETECT_PORTS = (8088, 8090, 8787, 11434)
DETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
TIMEOUT = 180.0
_SUFFIXES = ("/v1/chat/completions", "/v1/models", "/healthz", "/v1")


def normalize_url(pasted: str) -> str:
    """Any pasted runtime address -> ``<scheme>://<host[:port]>/v1``."""
    text = (pasted or "").strip()
    if not text:
        return DEFAULT_URL
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if not parts.netloc:
        raise ValueError(f"not a runtime address: {pasted!r}")
    path = parts.path.rstrip("/")
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)].rstrip("/")
                changed = True
    return f"{parts.scheme}://{parts.netloc}{path}/v1"


@dataclass(frozen=True)
class ProviderStatus:
    state: str  # online | unauthorized | offline
    url: str
    models: tuple[str, ...] = ()
    detail: str = ""


class MeterlessProvider:
    def __init__(self, url: str = DEFAULT_URL, token: str = "", *, model: str = "meterless/auto", transport: httpx.BaseTransport | None = None) -> None:
        if token.strip() == PLACEHOLDER_TOKEN:
            raise ValueError("rt-local is a placeholder; use the token from <stateDir>/proxy-token")
        self.url = normalize_url(url)
        self.token = token.strip()
        self.model = model
        self._transport = transport

    def _headers(self, chat: bool = False) -> dict[str, str]:
        h = {"meterless-app": APP_ID}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if chat:
            h["meterless-scope"] = "user"
        return h

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(TIMEOUT), transport=self._transport)

    def status(self) -> ProviderStatus:
        try:
            with self._client() as c:
                r = c.get(self.url + "/models", headers=self._headers())
        except httpx.HTTPError as e:
            return ProviderStatus("offline", self.url, detail=str(e))
        if r.status_code in (401, 403):
            return ProviderStatus("unauthorized", self.url, detail=f"HTTP {r.status_code}")
        if r.status_code != 200:
            return ProviderStatus("offline", self.url, detail=f"HTTP {r.status_code}")
        try:
            ids = tuple(str(m.get("id")) for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id"))
        except (ValueError, AttributeError):
            return ProviderStatus("offline", self.url, detail="GET /v1/models did not return a model list")
        return ProviderStatus("online", self.url, ids)

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "stream": False,
        }
        with self._client() as c:
            r = c.post(self.url + "/chat/completions", headers=self._headers(chat=True), json=body)
        if r.status_code in (401, 403):
            raise PermissionError("the runtime refused the token")
        r.raise_for_status()
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("the runtime returned no message") from e
        return content if isinstance(content, str) else json.dumps(content)


def candidate_urls() -> list[str]:
    return [f"http://{h}:{p}/v1" for p in DETECT_PORTS for h in DETECT_HOSTS]


def auto_detect(token: str = "", *, transport: httpx.BaseTransport | None = None, model: str = "meterless/auto") -> tuple[MeterlessProvider | None, list[ProviderStatus]]:
    """The first address that answers online or unauthorized, in contract order."""
    seen = []
    for url in candidate_urls():
        p = MeterlessProvider(url, token, model=model, transport=transport)
        st = p.status()
        seen.append(st)
        if st.state in ("online", "unauthorized"):
            return p, seen
    return None, seen


def pick_model(available: tuple[str, ...], preferred: str) -> str:
    """Keep the preferred routing mode when offered; else the first meterless/* mode; else
    the first model id (a plain OpenAI-compatible server such as a local model runner)."""
    if preferred in available:
        return preferred
    for m in available:
        if m.startswith("meterless/"):
            return m
    return available[0] if available else preferred


SYSTEM = (
    "You fix SQL so a second engine returns exactly the same rows as the first. "
    "Reply with one SQL statement in Spark SQL inside a ```sql block and nothing else. "
    "Keep every table name exactly as written. Do not change what the query asks for."
)


def extract_sql(text: str) -> str | None:
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        return None
    return sql


class ModelAdvisor:
    """Asks the runtime for a rewrite. Returns None rather than raising when it has nothing."""

    def __init__(self, provider: MeterlessProvider) -> None:
        self.provider = provider
        self.last_prompt: str | None = None

    def prompt(self, *, query_id: str, target_sql: str, diff: dict[str, Any], target_error: str | None) -> str:
        facts = {k: diff.get(k) for k in ("status", "source_rows", "target_rows", "mismatched_columns", "type_mismatches", "max_rel_gap") if k in diff}
        lines = [
            f"Query {query_id} runs on the second engine as:",
            "```sql", target_sql, "```",
        ]
        if target_error:
            lines += ["The second engine refused it with:", target_error[:600]]
        else:
            lines += ["Compared with the first engine's answer:", json.dumps(facts, indent=1, default=str)]
        lines.append("Rewrite the query so the second engine returns the first engine's rows exactly.")
        return "\n".join(lines)

    def propose(self, *, query_id: str, target_sql: str, diff: dict[str, Any], target_error: str | None) -> str | None:
        self.last_prompt = self.prompt(query_id=query_id, target_sql=target_sql, diff=diff, target_error=target_error)
        return extract_sql(self.provider.complete(SYSTEM, self.last_prompt))


NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "nvidia/nvidia-nemotron-nano-9b-v2"


def model_provider(s, *, transport: httpx.BaseTransport | None = None) -> tuple[MeterlessProvider | None, ProviderStatus | None]:
    """The model route, in this order: an explicit METERLESS_URL; NVIDIA's hosted
    OpenAI-compatible endpoint when NVIDIA_API_KEY is set; otherwise a Meterless runtime
    found by auto-detect. Returns (provider when online, the last status seen)."""
    if s.meterless_url:
        p = MeterlessProvider(s.meterless_url, s.meterless_token, model=s.meterless_model, transport=transport)
    elif s.nvidia_api_key:
        model = s.meterless_model if s.meterless_model.startswith("nvidia/") else NVIDIA_DEFAULT_MODEL
        p = MeterlessProvider(NVIDIA_URL, s.nvidia_api_key, model=model, transport=transport)
    else:
        found, seen = auto_detect(s.meterless_token, transport=transport, model=s.meterless_model)
        if found is None:
            return None, (seen[-1] if seen else None)
        p = found
    st = p.status()
    if st.state != "online":
        return None, st
    if st.models and p.model not in st.models:
        nemotron = [m for m in st.models if "nemotron" in m.lower()]
        p.model = nemotron[0] if nemotron and not p.model.startswith("meterless/") else pick_model(st.models, p.model)
    return p, st
