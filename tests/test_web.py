"""The shipped web app: served by the API, and every API path it calls exists."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nirvana.api import create_app
from nirvana.config import settings

WEB = Path(__file__).resolve().parents[1] / "web" / "dist"
JS = (WEB / "assets" / "app.js").read_text()
CSS = (WEB / "assets" / "app.css").read_text()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NIRVANA_HOME", str(tmp_path))
    monkeypatch.setenv("NIRVANA_WEB", str(WEB))
    with TestClient(create_app(settings({"NIRVANA_HOME": str(tmp_path)}), mode="live")) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/app", "/app/overview", "/app/run", "/app/queries", "/app/q/q42", "/app/tables", "/app/receipt", "/app/workload", "/app/workload/q3", "/app/memory", "/app/history"])
def test_every_route_serves_the_app_shell(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert '<div id="root">' in r.text and "/assets/app.js" in r.text


@pytest.mark.parametrize("asset,ctype", [("/assets/app.js", "javascript"), ("/assets/app.css", "text/css"), ("/assets/fonts/Inter-latin.woff2", "font"), ("/assets/fonts/JetBrainsMono-latin.woff2", "font")])
def test_assets_are_served_locally(client, asset, ctype):
    r = client.get(asset)
    assert r.status_code == 200 and ctype in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/api/nope", "/api/runs/x/nope", "/api"])
def test_unknown_api_paths_are_404_not_the_app(client, path):
    assert client.get(path).status_code == 404


def test_no_font_or_script_from_a_cdn():
    html = (WEB / "index.html").read_text()
    for text in (html, CSS):
        assert "googleapis" not in text and "gstatic" not in text and "cdn" not in text.lower()


def _routes(client):
    return [r.path for r in client.app.routes if r.path.startswith("/api")]


API_CALLS = sorted(set(re.findall(r'api\(`?"?(/[a-z][^`"?$]*)', JS)))


def test_the_app_calls_a_known_set_of_endpoints():
    assert {"/health", "/status", "/runs", "/workload", "/memory"} <= set(API_CALLS)


@pytest.mark.parametrize("call", [
    "/api/health", "/api/status", "/api/runs", "/api/runs/{run_id}", "/api/runs/{run_id}/events", "/api/runs/{run_id}/stream",
    "/api/runs/{run_id}/judgements", "/api/runs/{run_id}/judgements/{query_id}", "/api/runs/{run_id}/alignment",
    "/api/runs/{run_id}/receipt", "/api/runs/{run_id}/recording", "/api/runs/{run_id}/cancel", "/api/workload", "/api/workload/{query_id}", "/api/memory",
])
def test_each_endpoint_the_app_uses_exists(client, call):
    assert call in _routes(client)


@pytest.mark.parametrize("verdict", ["move", "move_note", "rewrite", "keep", "blocked"])
def test_every_verdict_has_a_label_a_tag_and_a_cell_colour(verdict):
    assert re.search(rf"\b{verdict}: \"", JS)
    assert f".v-{verdict} " in CSS and f".cell.v-{verdict} " in CSS and f".c-{verdict} " in CSS


@pytest.mark.parametrize("status", ["aligned", "aligned_types", "not_aligned", "missing"])
def test_every_alignment_status_has_a_label_and_a_tag(status):
    assert re.search(rf"\b{status}: \"", JS) and f".a-{status} " in CSS


@pytest.mark.parametrize("kind", ["started", "ping", "aligned", "judging", "judged", "finished", "failed", "cancelled"])
def test_the_live_page_listens_for_every_run_event(kind):
    assert f'"{kind}"' in JS.split("es.addEventListener")[0].split("EventSource")[-1] or f'"{kind}", ' in JS or f'"{kind}"]' in JS


def test_every_css_variable_used_is_defined():
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", CSS + JS))
    assert used <= defined, used - defined


def test_the_page_never_prints_a_raw_absence():
    for bad in ('"undefined"', "'undefined'", '"NaN"'):
        assert bad not in JS
