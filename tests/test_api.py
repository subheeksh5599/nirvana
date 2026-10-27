import json
import time

import pytest
from fastapi.testclient import TestClient

from nirvana.api import create_app
from nirvana.config import settings
from nirvana.ledger import Ledger


@pytest.fixture
def env(tmp_path):
    return {
        "NIRVANA_HOME": str(tmp_path / "home"),
        "NIRVANA_RECORDINGS": str(tmp_path / "recordings"),
        "METERLESS_URL": "http://127.0.0.1:9/v1",  # nothing listens on the discard port
    }


@pytest.fixture
def client(env, monkeypatch):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("NIRVANA_WEB", "/nonexistent")
    app = create_app(settings(), mode="live")
    with TestClient(app) as c:
        yield c


def wait(client, run_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] and r["mode"] == "live" and r["recorded_runs"] == 0


def test_status_without_check_reports_configuration_only(client):
    r = client.get("/api/status").json()
    assert r["configured"] == {"databricks": False, "zetaris": False, "meterless": True, "nvidia": False, "openrouter": False}
    assert "engines" not in r
    assert r["locations"] == {"source": "samples.tpcds_sf1", "target": "TPCDS_DB"}


def test_status_check_reports_missing_engines(client):
    r = client.get("/api/status", params={"check": True}).json()
    assert r["engines"]["source"]["ok"] is False
    assert "needs host" in r["engines"]["source"]["detail"]
    assert r["engines"]["target"]["ok"] is False
    assert r["model"]["state"] == "offline"


def test_workload_lists_103(client):
    w = client.get("/api/workload").json()
    assert len(w) == 103
    q1 = w[0]
    assert q1["id"] == "q1" and q1["ordered"] is True and q1["limit"] == 100
    assert "store_returns" in q1["tables"]


def test_workload_query_detail(client):
    r = client.get("/api/workload/q42").json()
    assert "samples.tpcds_sf1.store_sales" in r["source_sql"]
    assert "TPCDS_DB.store_sales" in r["target_sql"]


def test_workload_unknown(client):
    assert client.get("/api/workload/q1000").status_code == 404


def test_live_run_without_engines_is_refused(client):
    r = client.post("/api/runs", json={"engines": "live"})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_bad_engines_value(client):
    assert client.post("/api/runs", json={"engines": "cloud"}).status_code == 400


def test_unknown_query_refused(client):
    assert client.post("/api/runs", json={"engines": "rehearsal", "queries": ["q0"]}).status_code == 400


def test_rehearsal_run_end_to_end(client):
    r = client.post("/api/runs", json={"engines": "rehearsal", "queries": ["q3", "q42", "q30"], "use_model": False, "use_memory": False})
    assert r.status_code == 202
    rid = r.json()["id"]
    assert r.json()["mode"] == "rehearsal"
    run = wait(client, rid)
    assert run["status"] == "done"
    assert run["summary"]["queries"] == 3
    assert run["source"]["name"] == "rehearsal-source"

    js = client.get(f"/api/runs/{rid}/judgements").json()
    assert [j["query_id"] for j in js] == ["q3", "q42", "q30"]
    assert js[2]["verdict"] == "blocked"
    assert client.get(f"/api/runs/{rid}/judgements", params={"verdict": "blocked"}).json()[0]["query_id"] == "q30"

    full = client.get(f"/api/runs/{rid}/judgements/q3").json()
    assert full["steps"][0]["name"] == "source"
    assert client.get(f"/api/runs/{rid}/judgements/q99").status_code == 404

    al = client.get(f"/api/runs/{rid}/alignment").json()
    assert {a["table"] for a in al} >= {"store_sales", "date_dim", "item"}

    ev = client.get(f"/api/runs/{rid}/events").json()
    kinds = [e["kind"] for e in ev]
    assert kinds[0] == "started" and kinds[-1] == "finished"
    assert kinds.count("judged") == 3
    later = client.get(f"/api/runs/{rid}/events", params={"after": ev[-2]["seq"]}).json()
    assert len(later) == 1

    rc = client.get(f"/api/runs/{rid}/receipt").json()
    assert len(rc["sha256"]) == 64 and len(rc["receipt"]["items"]) == 3

    stream = client.get(f"/api/runs/{rid}/stream")
    assert "event: started" in stream.text and "event: end" in stream.text

    rec = client.get(f"/api/runs/{rid}/recording")
    assert rec.headers["content-disposition"].endswith(f'{rid}.json"')
    assert rec.json()["format"] == "nirvana-recording/1"

    assert any(x["id"] == rid for x in client.get("/api/runs").json())
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 409


def test_unknown_run_404s(client):
    for path in ("", "/alignment", "/judgements", "/events", "/receipt", "/recording"):
        assert client.get(f"/api/runs/nope{path}").status_code == 404


def test_recorded_mode_serves_recordings_and_refuses_runs(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recs"
    rec_dir.mkdir()
    led = Ledger(tmp_path / "src.sqlite")
    led.create_run("run-x", mode="live", source={"name": "databricks"}, target={"name": "zetaris"}, query_ids=["q1"])
    led.put_judgement("run-x", 0, {"query_id": "q1", "verdict": "move", "cause": None, "note": "same", "source_sql": "a", "target_sql": "b", "comparison": {"source_fp": "f", "target_fp": "f"}})
    led.finish_run("run-x", "done", {"queries": 1})
    (rec_dir / "run-x.json").write_text(json.dumps(led.export("run-x")))
    monkeypatch.setenv("NIRVANA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NIRVANA_RECORDINGS", str(rec_dir))
    monkeypatch.setenv("NIRVANA_WEB", "/nonexistent")
    with TestClient(create_app(settings(), mode="recorded")) as c:
        assert c.get("/api/health").json() == {"ok": True, "version": "0.1.0", "mode": "recorded", "recorded_runs": 1}
        runs = c.get("/api/runs").json()
        assert runs[0]["id"] == "run-x" and runs[0]["recorded"] is True
        assert c.get("/api/runs/run-x/judgements").json()[0]["verdict"] == "move"
        r = c.post("/api/runs", json={"engines": "rehearsal"})
        assert r.status_code == 409 and "recorded" in r.json()["detail"]


def test_bad_mode():
    with pytest.raises(ValueError):
        create_app(settings({}), mode="cloud")


def test_spa_served_when_built(tmp_path, monkeypatch, env):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>nirvana</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (web / "logo.svg").write_text("<svg/>")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("NIRVANA_WEB", str(web))
    with TestClient(create_app(settings(), mode="live")) as c:
        assert c.get("/").text == "<html>nirvana</html>"
        assert c.get("/runs/abc").text == "<html>nirvana</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/logo.svg").text == "<svg/>"
        assert c.get("/api/health").json()["ok"]


def test_openapi_lists_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/api/runs", "/api/runs/{run_id}/judgements/{query_id}", "/api/runs/{run_id}/stream", "/api/workload", "/api/status", "/api/memory"):
        assert p in paths
