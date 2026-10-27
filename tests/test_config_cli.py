import json
import os

import pytest

from nirvana import cli
from nirvana.config import load_env_file, parse_env_text, settings


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A=1", {"A": "1"}),
        ("export B=two", {"B": "two"}),
        ('C="quoted value"', {"C": "quoted value"}),
        ("D='single'", {"D": "single"}),
        ("E=x # comment", {"E": "x"}),
        ("# comment\n\nF=1", {"F": "1"}),
        ("not a line", {}),
        ("G=", {"G": ""}),
        ("H = spaced ", {"H": "spaced"}),
        ("URL=http://h:1/a#b", {"URL": "http://h:1/a#b"}),
    ],
)
def test_parse_env_text(text, expected):
    assert parse_env_text(text) == expected


def test_load_env_file_does_not_override(tmp_path, monkeypatch):
    f = tmp_path / ".env.local"
    f.write_text("NV_TEST_A=file\nNV_TEST_B=file\n")
    monkeypatch.setenv("NV_TEST_A", "shell")
    monkeypatch.delenv("NV_TEST_B", raising=False)
    assert load_env_file(f) == f
    assert os.environ["NV_TEST_A"] == "shell" and os.environ["NV_TEST_B"] == "file"
    monkeypatch.delenv("NV_TEST_B")


def test_load_env_file_missing(tmp_path):
    assert load_env_file(tmp_path / "nope") is None


def test_settings_defaults():
    s = settings({})
    assert (s.source, s.target) == ("databricks", "zetaris")
    assert (s.source_location, s.target_location) == ("samples.tpcds_sf1", "TPCDS_DB")
    assert s.zetaris_rest_url == "http://localhost:8888/api/v1.0"
    assert s.meterless_model == "meterless/auto"
    assert s.configured() == {"databricks": False, "zetaris": False, "meterless": False, "nvidia": False}
    assert s.ledger_path.name == "ledger.sqlite"


def test_settings_configured_flags_never_carry_values():
    s = settings({"DATABRICKS_HOST": "h", "DATABRICKS_TOKEN": "secret-token", "DATABRICKS_WAREHOUSE_ID": "w", "ZETARIS_API_KEY": "k"})
    flags = s.configured()
    assert flags == {"databricks": True, "zetaris": True, "meterless": False, "nvidia": False}
    assert "secret-token" not in json.dumps(flags)


def test_settings_numbers():
    s = settings({"NIRVANA_REHEARSAL_SCALE": "0.1", "NIRVANA_MAX_ROWS": "5"})
    assert s.rehearsal_scale == 0.1 and s.max_rows == 5


def test_cli_run_rehearsal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NIRVANA_HOME", str(tmp_path))
    rc = cli.main(["run", "--rehearsal", "--no-model", "--no-memory", "q3", "q30"])
    out = capsys.readouterr()
    assert rc == 0
    data = json.loads(out.out)
    assert data["status"] == "done" and data["summary"]["queries"] == 2
    assert "q30" in out.err and "blocked" in out.err


def test_cli_status_fails_without_engines(monkeypatch, capsys, tmp_path):
    for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_WAREHOUSE_ID", "ZETARIS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_env_file", lambda *a, **k: None)
    rc = cli.main(["status"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["source"]["ok"] is False


def test_cli_export(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NIRVANA_HOME", str(tmp_path))
    cli.main(["run", "--rehearsal", "--no-model", "--no-memory", "q3"])
    rid = json.loads(capsys.readouterr().out)["run"]
    out = tmp_path / "rec.json"
    assert cli.main(["export", rid, "--out", str(out)]) == 0
    rec = json.loads(out.read_text())
    assert rec["format"] == "nirvana-recording/1" and rec["run"]["id"] == rid


def test_cli_requires_command():
    with pytest.raises(SystemExit):
        cli.main([])
