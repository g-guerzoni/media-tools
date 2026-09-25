# media-tools: container image and internal job service — design

**Date:** 2026-09-25
**Status:** draft. The owner has decided D-NET, the scope and the deploy boundary. The
decisions marked *proposed* in the table below still wait on the owner.
**Scope:** one self-contained Docker image with no host dependencies, and a small job
service inside it that other apps on the same host can call. The image runs the same way
on `vps-remote-desktop` for the owner and the agents.

## Why this exists

A web app under development will use media-tools' tasks to process files. Today
media-tools is a CLI that relies on its host: Calibre comes from the machine, `op`
resolves secrets, and the tool is installed in a per-checkout `.venv` that is not even on
`PATH` on `vps-remote-desktop`. To run it in prod it has to become:

- **self-contained**: every binary it calls ships in the image;
- **callable by other apps**: a CLI has no way to take work from another container;
- **restricted**: callers are only apps on the same host. media-tools is never exposed
  to the web.

## Decisions

| ID | Question | Decision | Status |
| --- | --- | --- | --- |
| D-NET | What may prod reach on the internet? | `download` is **disabled in prod** and documented as such. It stays enabled for local runs. Prod's remaining egress is OpenRouter and Calibre's metadata providers. | owner, 2026-09-25 |
| D-FEAT | Which features does prod keep? | Every task except `download` and `ebook kindle` (no USB in a container). | owner, 2026-09-25 |
| D-EXPOSE | Who can reach it? | Apps on the same host only, over an internal Docker network. No Caddy route, no host port. | owner, 2026-09-25 |
| D-DEPLOY | Does this work deploy to `vps-default`? | **No.** The owner owns capacity and deployment there. This work ends at an image in GHCR plus a reviewed compose file. | owner, 2026-09-25 |
| D-API | How do callers submit work? | An HTTP job API built on the Python standard library, served by a new `media-tools serve` subcommand. | *proposed* |
| D-FILES | How do files move? | Through a shared named volume. There is no upload endpoint. | *proposed* |
| D-AUTH | How is a caller identified? | One bearer token per calling app, each read from its own secret file. | *proposed* |
| D-RET | How long is data kept? | 72 h per job, plus a hard size cap on the volume. | *proposed* |
| D-LOCAL | How does the box run it? | The native `.venv` put on `PATH` (full features, including Kindle and `download`), plus the same image through a wrapper script. | *proposed* |

## Architecture

```
caller app A ─┐                          ┌──────────────── media-tools container ────────────────┐
caller app B ─┼── media net (internal ──▶│ media-tools serve  (stdlib HTTP, :8080, no host port)   │
              │   to the host)           │   ├─ auth: bearer token per caller                    │
              │                          │   ├─ job queue (in-process, MAX_JOBS workers)         │
              │                          │   └─ each job = subprocess `media-tools <task> --json`│
              │                          └──────────────────────┬────────────────────────────────┘
              └──── mounts the same named volume ───────────────┤ /data
                                                                ├─ in/<caller>/...   (caller writes)
                                                                ├─ out/<caller>/     (per-caller output root)
                                                                └─ jobs/<id>/        (request, events.jsonl, status)
```

### The image

- **Base:** `ubuntu:24.04`, **pinned by digest**. It has the same Calibre (`7.6.0+ds-1build1`)
  and Python (`3.12`) as `vps-remote-desktop`, where the full suite passes with Calibre
  present, so the image starts from a combination already known to be green.
- **Calibre** comes from the Ubuntu archive (`--no-install-recommends`), as MAC_VPS_SEC_REVIEW
  approved. Upstream's installer is never used.
- **ffmpeg** stays the `imageio-ffmpeg` wheel's bundled binary (80 MB), so the image
  runs the same code path as every other install. No distro ffmpeg.
- **Python dependencies** are installed from `requirements.lock`, generated with
  `uv pip compile --generate-hashes` from `pyproject.toml` constrained by
  `constraints.txt`, and installed with `pip install --require-hashes`. What hashes add
  on top of `==` pins is completeness: pip fails if any requirement, including an
  indirect one, is left unpinned. CI fails when the lock and `constraints.txt` disagree.
- **Why pinning is safe to hold:** the `latest-deps` CI job installs unpinned and is
  allowed to fail, so upstream breakage shows up without anyone having to remember to
  look. It does **not** show a yt-dlp *security* fix. yt-dlp releases are watched
  separately. In prod yt-dlp is only reachable through `download`, which is disabled.
- **Not in the image:** `op` (secrets arrive as files), `udisksctl`/`calibre-debug` use
  (the Kindle commands are disabled), and build tools (multi-stage build).
- **Runtime user:** a dedicated uid/gid `10001`. Never 1000, which is `deploy` on
  `vps-default`.
- **Size** is measured during implementation. Calibre pulls in Qt, and the estimate is
  1.2–1.5 GB. `ebook-convert` in a headless container may need `QT_QPA_PLATFORM=offscreen`;
  verify this rather than assume it.

### Disabling a task: `MEDIA_TOOLS_DISABLED_TASKS`

A new environment variable holds a comma-separated list: `download`, `ebook-kindle`. The
prod image sets `MEDIA_TOOLS_DISABLED_TASKS=download,ebook-kindle`, and the local run
leaves it unset.

- `cli.main` checks the list before dispatch. A disabled task raises `UsageError`, so it
  gets the normal `error` (code `usage`) plus `result` pair and exit 2. The message
  names the variable and says the task is disabled in this deployment. The code
  registry does not change.
- `formats --json` and `doctor --json` report the disabled tasks, so a caller can find
  out without trying.
- `serve` refuses a job for a disabled task at submission, with HTTP 403, before any
  subprocess starts.

This lives in the tool rather than only in the API, so a `docker exec` into the prod
container cannot run `download` either.

### Secrets: `*_FILE` variables

`openrouter.resolve_key` gains a fourth source, tried first: `OPENROUTER_API_KEY_FILE`,
a path whose content is the key. It follows the Docker file `secrets:` layout of spec
036 on `vps-default`: `/srv/secrets/media-tools/<name>`, directory
`root:<group> 0750`, files `root:<group> 0440`, reaching the container through
`group_add`. Caller tokens use the same mechanism. The key is a **media-tools-specific
vault item**, never wf's. `doctor` reports whether the file is readable and never
prints its content.

### `media-tools serve`: the job API

This is a new subcommand built on `http.server.ThreadingHTTPServer`, with **no new
dependency**. It is not a web framework and does not need to be one: four JSON
endpoints and no body parser beyond `json.loads` with a size cap.

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/v1/jobs` | Submit `{task, inputs[], options{}}`. Returns `202 {id}` |
| `GET` | `/v1/jobs/{id}` | Status, plus the final `result` event once finished |
| `GET` | `/v1/jobs/{id}/events?after=N` | The job's JSON Lines events from line N, so callers can poll progress |
| `DELETE` | `/v1/jobs/{id}` | Cancel: SIGINT to the subprocess, so the tool's own exit-130 path runs |
| `GET` | `/healthz` | `200` when the server is up and the startup `doctor` found nothing `missing` |

Rules:

- **A job is a subprocess of the real CLI.** The CLI stays the single source of truth,
  and its JSON Lines contract becomes the API's event format unchanged.
- **Requests are structured, not argv.** `options` keys map to flags through an
  allowlist built from the task's own argparse parser. Unknown keys are rejected.
  `-o/--output-dir` is never accepted from a caller: the server forces
  `-o /data/out/<caller>`, so each caller has its own output root, batch namespace
  and `.cache/`. No caller sees another's files or LLM cache.
- **Paths are confined.** Every input must resolve, after following symlinks, inside
  `/data/in/<caller>/`. `--list` files are checked the same way, including the paths
  they name.
- **Concurrency:** `MEDIA_TOOLS_MAX_JOBS` (default 1) jobs run at once; the rest queue.
  Inside a job, `--workers` is capped by `MEDIA_TOOLS_MAX_WORKERS` (default 1), so one
  ebook build cannot take every core.
- **Timeout:** `MEDIA_TOOLS_JOB_TIMEOUT` (default 6 h) sends SIGINT, then SIGKILL after
  a grace period.
- **Restart:** job state lives in `/data/jobs/<id>/`. On startup a job found `running` is
  marked `interrupted`. Re-submitting the same request resumes it, because the default
  batch name is a hash of task, options and inputs.
- **Auth:** `Authorization: Bearer <token>`, compared with `hmac.compare_digest` against
  one token per caller. The caller's name comes from its token file's name
  (`/run/secrets/caller-<name>`), and that name is the `<caller>` in every path above.
  A request without a valid token gets 401, and a caller can only see its own jobs.

### Files, retention and the size bound

- **Volume:** `media-tools_data`, mounted at `/data`. A caller mounts the same volume
  and writes its inputs under `in/<caller>/`.
- **Retention:** a janitor thread in `serve` deletes `jobs/<id>/` and that job's batch
  directory `MEDIA_TOOLS_RETENTION_HOURS` (default 72) after the job finishes. It
  deletes anything under `in/` older than the same limit.
- **Size bound:** `MEDIA_TOOLS_MAX_DATA_BYTES`. When `/data` is over it, `POST /v1/jobs`
  returns `507` and no job starts. The janitor runs first. Nothing else watches volume
  growth on the host, so the service bounds itself.
- **Not backed up.** Everything in the volume is transient working data by design.
  Callers keep their own copies of anything they need.

This is the one place media-tools deletes its own output. The CLAUDE.md rule "never
edit files under `media/`" still binds agents; the janitor is the service's own
lifecycle, and it is scoped to `/data`.

### Container hardening

Every item below is required by `vps-gate box` or proposed on top of it:

```yaml
user: "10001:10001"
read_only: true
tmpfs: ["/tmp:size=2g,mode=1777"]          # Calibre scratch + HOME
environment: {HOME: /tmp/home, MEDIA_TOOLS_OUT: /data/out}
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
mem_limit: <measured>                     # see "Resources"
memswap_limit: <same as mem_limit>        # no swap: a CPU-bound batch job that pages
                                          # slows the live app next to it; an OOM kill
                                          # is a clean, retryable job failure instead
cpus: "1.0"                               # leaves a core for the host's web app
pids_limit: <measured>
restart: unless-stopped
healthcheck: {test: ["CMD", "python3", "-c", "...GET http://127.0.0.1:8080/healthz..."]}
image: ghcr.io/g-guerzoni/media-tools@sha256:<digest>
networks: [media]                         # no ports:
```

### Resources

Limits come from **measurement, not estimates**. During implementation, the peak RSS
and process count of each representative job are measured inside the image with
`MAX_JOBS=1` and `MAX_WORKERS=1`:

- `compress` of a 1080p, 10-minute clip;
- `split` of a 1 GB file;
- `ebook build --no-llm` over 200 mixed-format books;
- `ebook convert` of the largest PDF in the test library.

`mem_limit` = the largest peak × 1.25, rounded up to 128 MiB. The measurements and the
resulting numbers are recorded in this spec before any compose file is reviewed. The
owner decides where that memory comes from on `vps-default`.

### Network

`media` is a dedicated Docker network whose members are media-tools and the registered
callers. It **cannot be `Internal=true`**, because OpenRouter and the metadata
providers need egress. That is accepted under D-NET, since no arbitrary-URL fetcher
exists in prod. Membership needs enforcement, not convention: a
`gate-media-consumers` registry checked the way rule N6 checks `ai`. That is a
`vps-gate` change for MAC_VPS_SEC_REVIEW to review separately. So is registering a
service that has no hostname or edge route, which the current `gate-registry` schema
cannot express.

## Local run on `vps-remote-desktop`

Two ways, for two jobs:

1. **Native (full features):** `~/.local/bin/media-tools` → a symlink to this
   checkout's `.venv/bin/media-tools`. The owner and the agents get every task,
   including `download` and `ebook kindle`, with output in the checkout's `media/`,
   exactly as the docs already describe. This is a new host file, so in the same change
   it goes into `/CLAUDE.md`'s `gate-inventory` and literally into `rdesk-backup`'s
   `CANDIDATES`, with `rdesk-gate` at 0 fail. **That is a box change and needs the
   owner's approval.**
2. **Image (prod parity):** `scripts/media-tools-docker`, in the repo, runs the image
   with the current directory mounted at `/work`, `MEDIA_TOOLS_OUT=/work/media` and
   `MEDIA_TOOLS_DISABLED_TASKS` unset. So `download` works here too. `compose.local.yml`
   runs `serve` with its port published on `127.0.0.1` only, as the box's
   `desktop-ingress-guard` requires, so a local app can be developed against the real
   API.

## CI

- **Build** the image on every push, with its test stage running the offline suite
  inside it. That includes the Calibre-gated tests, which run nowhere in CI today.
- **Push** `ghcr.io/g-guerzoni/media-tools:<git sha>` on `main` only, and print the
  digest.
- **Check** `requirements.lock` against `constraints.txt`.
- The existing Python matrix and the `latest-deps` canary stay as they are.
- No deploy job. See D-DEPLOY.

## Testing

- `MEDIA_TOOLS_DISABLED_TASKS`: a disabled task exits 2 with `error`+`result` and runs
  nothing; `formats`/`doctor` report it; an unset variable changes nothing.
- `OPENROUTER_API_KEY_FILE`: it wins over the other sources; an unreadable file is
  `config_missing`, never a crash; the key never appears in any output.
- `serve`, without Docker, by starting the server on an ephemeral port in-process:
  - a request without a token gets 401;
  - a caller cannot read another caller's job;
  - an input path escaping `in/<caller>/` through `..` or a symlink is rejected;
  - an unknown option key is rejected, and a forced `-o` cannot be overridden;
  - a disabled task gets 403;
  - cancellation reaches the subprocess, and its `result` shows exit code 130;
  - a job found `running` at startup becomes `interrupted`;
  - retention deletes an expired job and nothing newer;
  - the size bound returns 507.
- **Image smoke test** in CI: `doctor --json` inside the image reports no `missing`, and
  one `compress`, one `convert --to epub` and one `ebook build --no-llm` succeed as uid
  10001 on a read-only root.

## Out of scope

- Deploying to `vps-default`, choosing its capacity, and its deploy wrapper (the owner).
- The `vps-gate` changes: `gate-media-consumers` enforcement, and a registry shape for
  a service with no edge route (MAC_VPS_SEC_REVIEW reviews these separately).
- `download` in prod. If it is ever needed, the path is MAC_VPS_SEC_REVIEW's split
  design: an `api` container with no egress and a `fetcher` container callers cannot
  reach, from the same image. The code here does not rule it out.
- Kindle support in a container.
- Backing up the data volume.
