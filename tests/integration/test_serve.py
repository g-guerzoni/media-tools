"""`media-tools serve`, driven over real HTTP on an ephemeral port, running real jobs."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from media_tools.cli import build_parser
from media_tools.core import disabled
from media_tools.tasks.serve.http import Server, TokenError, load_tokens
from media_tools.tasks.serve.jobs import Settings, Store

TOKENS = {"app1": "a" * 40, "app2": "b" * 40}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv(disabled.ENV_VAR, raising=False)
    monkeypatch.setattr(disabled, "BAKED_PATH", tmp_path / "no-baked-file")
    data = tmp_path / "data"
    for sub in ("in", "out", "jobs", "tmp"):
        (data / sub).mkdir(parents=True)
    store = Store(Settings(data=data, sweep_s=3600))
    store.start_workers()
    server = Server(
        ("127.0.0.1", 0), store=store, tokens=TOKENS, parser=build_parser(), problems=[]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _call(url, method="GET", body=None, caller="app1"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if caller:
        request.add_header("Authorization", f"Bearer {TOKENS[caller]}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _wait(base, job_id, caller="app1", timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, record = _call(f"{base}/v1/jobs/{job_id}", caller=caller)
        if record["status"] not in ("queued", "running"):
            return record
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish")


def _audio(make_audio, store, caller="app1", name="talk.m4a"):
    store.prepare_caller(caller)
    rel = store.input_root(caller).relative_to(store.settings.data.parent)
    return make_audio(seconds=1, name=f"{rel}/{name}")


def test_a_job_runs_end_to_end(service, make_audio):
    _, store, base = service
    _audio(make_audio, store)
    status, body = _call(
        f"{base}/v1/jobs",
        "POST",
        {"task": "convert", "inputs": ["talk.m4a"], "options": {"to": "mp3"}},
    )
    assert status == 202
    record = _wait(base, body["id"])
    assert record["status"] == "done"
    assert record["exit_code"] == 0
    assert record["result"]["ok"] is True
    output = record["result"]["outputs"][0]
    assert output.startswith(str(store.output_root("app1")))
    assert "argv" not in record  # server-side paths stay server-side
    _, page = _call(f"{base}/v1/jobs/{body['id']}/events?after=0")
    assert page["events"][0]["type"] == "start"
    assert page["events"][-1]["type"] == "result"
    _, rest = _call(f"{base}/v1/jobs/{body['id']}/events?after={page['next']}")
    assert rest["events"] == []
    assert not store.scratch(body["id"]).exists()


def test_no_token_is_401_and_a_wrong_one_too(service):
    _, _, base = service
    assert _call(f"{base}/v1/jobs", "POST", {"task": "compress"}, caller=None)[0] == 401
    request = urllib.request.Request(f"{base}/v1/jobs/0123456789abcdef")
    request.add_header("Authorization", "Bearer " + "c" * 40)
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=10)
    assert info.value.code == 401


def test_a_caller_cannot_see_or_cancel_another_callers_job(service, make_audio):
    _, store, base = service
    _audio(make_audio, store)
    _, body = _call(f"{base}/v1/jobs", "POST", {"task": "convert", "inputs": ["talk.m4a"]})
    for method in ("GET", "DELETE"):
        status, _ = _call(f"{base}/v1/jobs/{body['id']}", method, caller="app2")
        assert status == 404
    assert _call(f"{base}/v1/jobs/{body['id']}/events", caller="app2")[0] == 404
    _wait(base, body["id"])


def test_a_bad_request_is_400_and_starts_nothing(service):
    _, store, base = service
    status, body = _call(
        f"{base}/v1/jobs", "POST", {"task": "compress", "inputs": ["../app2/x.mp4"]}
    )
    assert status == 400
    assert "outside your input directory" in body["error"]
    assert list((store.settings.data / "jobs").iterdir()) == []


def test_a_disabled_task_is_403(service, monkeypatch):
    _, _, base = service
    monkeypatch.setenv(disabled.ENV_VAR, "download")
    status, body = _call(
        f"{base}/v1/jobs", "POST", {"task": "download", "inputs": ["https://example.com/v"]}
    )
    assert status == 403
    assert "disabled" in body["error"]


def test_over_the_size_cap_is_507(service, make_audio):
    _, store, base = service
    _audio(make_audio, store)
    store.settings.max_data_bytes = 1
    store.sweep()
    status, _ = _call(f"{base}/v1/jobs", "POST", {"task": "convert", "inputs": ["talk.m4a"]})
    assert status == 507


def test_healthz_fails_until_the_janitor_has_run_and_when_it_goes_stale(service):
    _, store, base = service
    request = urllib.request.Request(f"{base}/healthz")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=10)
    assert info.value.code == 503
    store.sweep()
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
    store.janitor_last_run -= 3 * store.settings.sweep_s
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=10)
    body = json.loads(info.value.read())
    assert body["janitor_last_run"] is not None


def test_cancelling_a_queued_job_never_runs_it(service, make_audio):
    _, store, base = service
    _audio(make_audio, store)
    # One worker: hold it with a first job, then cancel the second while it queues.
    _, first = _call(f"{base}/v1/jobs", "POST", {"task": "convert", "inputs": ["talk.m4a"]})
    _, second = _call(
        f"{base}/v1/jobs", "POST", {"task": "convert", "inputs": ["talk.m4a"],
                                    "options": {"force": True}}
    )  # fmt: skip
    status, record = _call(f"{base}/v1/jobs/{second['id']}", "DELETE")
    assert status == 202
    _wait(base, first["id"])
    final = _wait(base, second["id"])
    assert final["status"] in ("cancelled",)
    assert final["started_at"] is None


def test_recover_marks_running_interrupted_and_requeues_queued(tmp_path):
    data = tmp_path / "data"
    store = Store(Settings(data=data))
    running = store.submit("app1", {"task": "compress"}, ["compress"])
    queued = store.submit("app1", {"task": "compress"}, ["compress"])
    store._update(running["id"], status="running")
    fresh = Store(Settings(data=data))
    fresh.recover()
    assert fresh.read(running["id"])["status"] == "interrupted"
    assert fresh._queue.get_nowait() == queued["id"]


def test_retention_deletes_what_expired_and_nothing_newer(tmp_path):
    data = tmp_path / "data"
    store = Store(Settings(data=data, retention_s=3600))
    out = store.output_root("app1")
    old_batch, shared_batch = out / "old", out / "shared"
    for batch in (old_batch, shared_batch):
        batch.mkdir(parents=True)
        (batch / "run.json").write_text("{}")

    def job(batch, finished_at):
        record = store.submit("app1", {}, [])
        store._queue.get_nowait()
        return store._update(
            record["id"], status="done", finished_at=finished_at,
            result={"run_file": str(batch / "run.json")},
        )  # fmt: skip

    long_ago = "2000-01-01T00:00:00Z"
    expired = job(old_batch, long_ago)
    expired_shared = job(shared_batch, long_ago)
    recent_shared = job(shared_batch, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    store.sweep()
    assert not old_batch.exists()
    assert store.read(expired["id"]) is None
    assert shared_batch.exists(), "a batch a live job still names must survive"
    assert store.read(expired_shared["id"]) is None
    assert store.read(recent_shared["id"])["status"] == "done"


def test_a_busy_caller_keeps_its_batches(tmp_path):
    data = tmp_path / "data"
    store = Store(Settings(data=data, retention_s=3600))
    batch = store.output_root("app1") / "old"
    batch.mkdir(parents=True)
    record = store.submit("app1", {}, [])
    store._update(record["id"], status="done", finished_at="2000-01-01T00:00:00Z",
                  result={"run_file": str(batch / "run.json")})  # fmt: skip
    store.submit("app1", {}, [])  # still queued: its batch is not known yet
    store.sweep()
    assert batch.exists()


def test_tokens_come_from_caller_files_and_are_validated(tmp_path):
    (tmp_path / "caller-app1").write_text("x" * 40 + "\n")
    (tmp_path / "unrelated").write_text("ignored")
    assert load_tokens(tmp_path) == {"app1": "x" * 40}
    (tmp_path / "caller-Bad_Name").write_text("x" * 40)
    with pytest.raises(TokenError, match="invalid caller name"):
        load_tokens(tmp_path)
    (tmp_path / "caller-Bad_Name").unlink()
    (tmp_path / "caller-short").write_text("tooshort")
    with pytest.raises(TokenError, match="at least 32"):
        load_tokens(tmp_path)
