import json

import httpx
import pytest

from nirvana.meterless import (
    APP_ID,
    DEFAULT_URL,
    MeterlessProvider,
    ModelAdvisor,
    auto_detect,
    candidate_urls,
    extract_sql,
    normalize_url,
    pick_model,
)


@pytest.mark.parametrize(
    "pasted,expected",
    [
        ("", DEFAULT_URL),
        ("127.0.0.1:8088", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/v1", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/chat/completions", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/v1/models", "http://127.0.0.1:8088/v1"),
        ("http://127.0.0.1:8088/healthz", "http://127.0.0.1:8088/v1"),
        ("192.168.1.20:8088/v1", "http://192.168.1.20:8088/v1"),
        ("https://box.local/v1/models", "https://box.local/v1"),
        ("localhost:11434", "http://localhost:11434/v1"),
        ("  http://[::1]:8790  ", "http://[::1]:8790/v1"),
        ("http://host/proxy/v1/chat/completions", "http://host/proxy/v1"),
    ],
)
def test_normalize_url(pasted, expected):
    assert normalize_url(pasted) == expected
    assert "/v1/v1" not in normalize_url(pasted)


def test_normalize_rejects_hostless():
    with pytest.raises(ValueError):
        normalize_url("http://")


def test_placeholder_token_refused():
    with pytest.raises(ValueError, match="placeholder"):
        MeterlessProvider(token="rt-local")


def prov(handler, token="tok", model="meterless/auto"):
    return MeterlessProvider("127.0.0.1:8088", token, model=model, transport=httpx.MockTransport(handler))


def test_status_online_lists_models():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "meterless/deep"}]})

    st = prov(handler).status()
    assert st.state == "online" and st.models == ("meterless/auto", "meterless/deep")
    assert seen["path"] == "/v1/models"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["headers"]["meterless-app"] == APP_ID
    assert "meterless-scope" not in seen["headers"]


@pytest.mark.parametrize("code", [401, 403])
def test_status_unauthorized_is_not_offline(code):
    assert prov(lambda r: httpx.Response(code)).status().state == "unauthorized"


@pytest.mark.parametrize("code", [404, 500, 502])
def test_status_other_codes_are_offline(code):
    assert prov(lambda r: httpx.Response(code)).status().state == "offline"


def test_status_network_error_is_offline():
    def handler(req):
        raise httpx.ConnectError("refused")

    st = prov(handler).status()
    assert st.state == "offline" and "refused" in st.detail


def test_status_bad_json_is_offline():
    assert prov(lambda r: httpx.Response(200, text="not json")).status().state == "offline"


def test_no_token_sends_no_authorization():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"data": []})

    prov(handler, token="").status()
    assert "authorization" not in seen


def test_complete_sends_contract_headers_and_body():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    out = prov(handler, model="meterless/deep").complete("sys", "user")
    assert out == "hello"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["headers"]["meterless-scope"] == "user"
    assert seen["headers"]["meterless-app"] == APP_ID
    assert seen["body"]["model"] == "meterless/deep"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert isinstance(seen["body"]["messages"][1]["content"], str)
    assert seen["body"]["stream"] is False


def test_complete_unauthorized():
    with pytest.raises(PermissionError):
        prov(lambda r: httpx.Response(401)).complete("s", "u")


def test_complete_no_message():
    with pytest.raises(ValueError):
        prov(lambda r: httpx.Response(200, json={"choices": []})).complete("s", "u")


def test_complete_server_error():
    with pytest.raises(httpx.HTTPStatusError):
        prov(lambda r: httpx.Response(500)).complete("s", "u")


def test_candidate_urls_follow_contract_order():
    urls = candidate_urls()
    assert urls[0] == "http://127.0.0.1:8088/v1"
    assert urls[2] == "http://[::1]:8088/v1"
    assert "http://localhost:11434/v1" in urls
    assert len(urls) == 12


def test_auto_detect_returns_first_reachable():
    def handler(req):
        if req.url.port == 8790 or req.url.port == 8787:
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        raise httpx.ConnectError("closed")

    p, seen = auto_detect("tok", transport=httpx.MockTransport(handler))
    assert p is not None and p.url == "http://127.0.0.1:8787/v1"
    assert [s.state for s in seen][-1] == "online"
    assert all(s.state == "offline" for s in seen[:-1])


def test_auto_detect_accepts_unauthorized_hit():
    def handler(req):
        if req.url.port == 8090:
            return httpx.Response(401)
        raise httpx.ConnectError("closed")

    p, seen = auto_detect("", transport=httpx.MockTransport(handler))
    assert p is not None and p.url.endswith(":8090/v1")
    assert seen[-1].state == "unauthorized"


def test_auto_detect_none():
    def handler(req):
        raise httpx.ConnectError("closed")

    p, seen = auto_detect("", transport=httpx.MockTransport(handler))
    assert p is None and len(seen) == 12


@pytest.mark.parametrize(
    "available,preferred,expected",
    [
        (("meterless/auto", "meterless/deep"), "meterless/deep", "meterless/deep"),
        (("meterless/fast",), "meterless/auto", "meterless/fast"),
        (("nemotron-mini:latest",), "meterless/auto", "nemotron-mini:latest"),
        ((), "meterless/auto", "meterless/auto"),
        (("x", "meterless/cheap"), "meterless/auto", "meterless/cheap"),
    ],
)
def test_pick_model(available, preferred, expected):
    assert pick_model(available, preferred) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("Here:\n```sql\nSELECT a FROM t;\n```\nDone", "SELECT a FROM t"),
        ("```\nWITH x AS (SELECT 1) SELECT * FROM x\n```", "WITH x AS (SELECT 1) SELECT * FROM x"),
        ("SELECT 2", "SELECT 2"),
        ("I cannot help", None),
        ("```sql\nDROP TABLE t\n```", None),
        ("```SQL\nselect 3\n```", "select 3"),
    ],
)
def test_extract_sql(text, expected):
    assert extract_sql(text) == expected


def test_advisor_prompt_and_proposal():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "```sql\nSELECT CAST(a AS DOUBLE) / b FROM t\n```"}}]})

    adv = ModelAdvisor(prov(handler))
    out = adv.propose(query_id="q9", target_sql="SELECT a / b FROM t", diff={"status": "different", "mismatched_columns": ["r"], "secret": "no"}, target_error=None)
    assert out == "SELECT CAST(a AS DOUBLE) / b FROM t"
    user = seen["body"]["messages"][1]["content"]
    assert "q9" in user and "SELECT a / b FROM t" in user and "mismatched_columns" in user
    assert "secret" not in user


def test_advisor_prompt_includes_engine_error():
    adv = ModelAdvisor(prov(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "no"}}]})))
    assert adv.propose(query_id="q1", target_sql="SELECT x", diff={}, target_error="UNRESOLVED_ROUTINE: foo") is None
    assert "UNRESOLVED_ROUTINE" in adv.last_prompt


def _settings(**env):
    from nirvana.config import settings

    return settings(env)


def test_model_provider_prefers_explicit_url():
    from nirvana.meterless import model_provider

    def handler(req):
        assert req.url.host == "box.local"
        return httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"}]})

    p, st = model_provider(_settings(METERLESS_URL="box.local:8088", NVIDIA_API_KEY="nv"), transport=httpx.MockTransport(handler))
    assert p is not None and p.url == "http://box.local:8088/v1" and st.state == "online"
    assert p.model == "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"


def test_model_provider_uses_nvidia_key_and_picks_a_nemotron():
    from nirvana.meterless import NVIDIA_URL, model_provider

    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": [{"id": "meta/llama-3.1-8b-instruct"}, {"id": "nvidia/llama-3.3-nemotron-super-49b-v1.5"}]})

    p, st = model_provider(_settings(NVIDIA_API_KEY="nvapi-x"), transport=httpx.MockTransport(handler))
    assert p is not None and p.url == NVIDIA_URL
    assert seen["auth"] == "Bearer nvapi-x" and seen["url"] == NVIDIA_URL + "/models"
    assert p.model == "nvidia/llama-3.3-nemotron-super-49b-v1.5"


def test_model_provider_keeps_configured_nvidia_model():
    from nirvana.meterless import model_provider

    def handler(req):
        return httpx.Response(200, json={"data": [{"id": "nvidia/a-nemotron"}, {"id": "nvidia/b-nemotron"}]})

    p, _ = model_provider(_settings(NVIDIA_API_KEY="k", METERLESS_MODEL="nvidia/b-nemotron"), transport=httpx.MockTransport(handler))
    assert p.model == "nvidia/b-nemotron"


def test_model_provider_refused_key_returns_none():
    from nirvana.meterless import model_provider

    p, st = model_provider(_settings(NVIDIA_API_KEY="bad"), transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert p is None and st.state == "unauthorized"


def test_model_provider_auto_detect_when_nothing_configured():
    from nirvana.meterless import model_provider

    def handler(req):
        if req.url.port == 11434:
            return httpx.Response(200, json={"data": [{"id": "nemotron-3-nano:4b"}, {"id": "nvidia/nemotron-3.5-lightning:free"}]})
        raise httpx.ConnectError("closed")

    p, st = model_provider(_settings(), transport=httpx.MockTransport(handler))
    assert p is not None and p.url.endswith(":11434/v1") and p.model == "nvidia/nemotron-3.5-lightning:free"


def _runtime(models):
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [{"id": m} for m in models]}))


@pytest.mark.parametrize("models", [
    ("meterless/auto", "meterless/local", "meterless/deep"),
    ("nemotron-3-nano:4b",),
    ("llama-3.2-3b", "qwen2.5-coder-3b"),
    ("nvidia/nemotron-3-super-120b-a12b",),
    ("nvidia/nemotron-3.5-content-safety:free",),
    (),
])
def test_runtime_without_a_relayed_free_nemotron_is_refused(models):
    from nirvana.meterless import model_provider

    p, st = model_provider(_settings(METERLESS_URL="127.0.0.1:8088"), transport=_runtime(models))
    assert p is None and st.state == "offline" and "Providers" in st.detail


@pytest.mark.parametrize("preferred", ["meterless/auto", "meterless/local", "nemotron-3-nano:4b"])
def test_a_local_model_named_in_settings_is_never_used(preferred):
    from nirvana.meterless import model_provider

    p, _ = model_provider(_settings(METERLESS_URL="127.0.0.1:8088", METERLESS_MODEL=preferred), transport=_runtime((preferred, "nvidia/nemotron-3-ultra-550b-a55b:free")))
    assert p.model == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_the_configured_relayed_nemotron_is_kept():
    from nirvana.meterless import model_provider

    want = "openrouter/nvidia/nemotron-3.5-lightning:free"
    p, _ = model_provider(_settings(METERLESS_URL="127.0.0.1:8088", METERLESS_MODEL=want), transport=_runtime(("nvidia/nemotron-3-ultra-550b-a55b:free", want)))
    assert p.model == want


def test_openrouter_only_routes_to_free_nemotron():
    from nirvana.meterless import OPENROUTER_URL, model_provider

    def handler(req):
        return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-super-120b-a12b"}, {"id": "nvidia/nemotron-3.5-content-safety:free"}, {"id": "nvidia/nemotron-3.5-lightning:free"}]})

    p, st = model_provider(_settings(OPENROUTER_API_KEY="sk-or"), transport=httpx.MockTransport(handler))
    assert p is not None and p.url == OPENROUTER_URL
    assert p.model == "nvidia/nemotron-3.5-lightning:free"


def test_openrouter_without_free_nemotron_is_offline():
    from nirvana.meterless import model_provider

    def handler(req):
        return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-super-120b-a12b"}]})

    p, st = model_provider(_settings(OPENROUTER_API_KEY="sk-or"), transport=httpx.MockTransport(handler))
    assert p is None and "no free Nemotron" in st.detail


@pytest.mark.parametrize(
    "models,preferred,expected",
    [
        (("nvidia/nemotron-3-ultra-550b-a55b:free",), "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-ultra-550b-a55b:free"),
        (("nvidia/nemotron-3-ultra-550b-a55b",), "nvidia/nemotron-3-ultra-550b-a55b", None),
        (("nvidia/nemotron-3.5-embed-1b:free", "nvidia/nemotron-3.5-lightning:free"), "x", "nvidia/nemotron-3.5-lightning:free"),
    ],
)
def test_free_nemotron(models, preferred, expected):
    from nirvana.meterless import free_nemotron

    assert free_nemotron(models, preferred) == expected
