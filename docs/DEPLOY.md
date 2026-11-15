# Deploying GigBid to a public URL

**Deployed:** web <https://gigbid-web-q921.onrender.com>, API
<https://gigbid-api-ta76.onrender.com>. Render appended a suffix to both
service names because the unsuffixed ones were already taken by other
accounts, so neither hostname matches the names in `render.yaml`.

Four things broke on the way there, recorded so the next person does not
rediscover them:

1. `pip install -e .` ran with only `pyproject.toml` copied, so setuptools
   could not find the package directories.
2. `apps/web/public/` did not exist, and Docker fails a build outright on a
   missing COPY source.
3. `email-validator` was never declared; it was in the dev venv as somebody
   else's transitive dependency, so the first clean container died at import.
4. Next's standalone server binds `localhost` unless `HOSTNAME=0.0.0.0` is
   set, which returns 502 with an empty body and a healthy-looking build log.

And one that is specific to this blueprint: `autoDeployTrigger: "off"` means
**saving an environment variable does not restart the service**. The value is
stored and the running process keeps the old one, which looks exactly like a
value that was never saved. `CORS_ORIGINS` is read once at import, so a
Manual Deploy is required after changing it.

Target: **Render free plan**. Two web services and one Postgres database,
no card required. Roughly 20 minutes, most of it waiting for image builds.

The compose file in `infra/` is unchanged and is still the topology the
marketplace-outage test exercises. This is a second, slimmer arrangement of the
same code for a host that bills per process.

---

## How nine services become two

Render's free plan covers web services only; background workers and private
services are paid. Rather than delete features to fit, two existing switches
are used:

| Compose service | On Render | How |
| --- | --- | --- |
| `api` | `gigbid-api` | web service |
| `mcp-portfolio`, `mcp-marketplace`, `mcp-workspace` | same process | `GIGBID_MCP_TRANSPORT_MODE=inprocess` — already the default for the API and the eval harness |
| `gigsim` | same process | `MOUNT_GIGSIM=1` mounts it at `/gigsim` |
| `worker` | same process, **off** | `RUN_WORKER_IN_API=1` to enable |
| `postgres` | `gigbid-db` | Render free Postgres |
| `redis` | dropped | the worker falls back to an asyncio scheduler with no broker |
| `web` | `gigbid-web` | web service |
| `ollama` | dropped | behind the `local-models` profile; the hosted provider is used instead |

Neither switch is a deployment hack. `inprocess` is the transport the API
already defaults to, and the mount works because `gigsim/server.py` builds its
world at import time.

---

## Steps

### 1. Push the repository

Render deploys from GitHub, so the repo must exist and be pushed first.

### 2. Create the blueprint

Render Dashboard → **New** → **Blueprint** → select the repo. It reads
`render.yaml` at the root and proposes two services and one database.

**Do not deploy yet.** One variable must be set before the first build.

### 3. Set the four dashboard values

Five variables are marked `sync: false` in `render.yaml` because they are
secrets or circular references, and Render prompts for them while applying the
blueprint.

**Do not assume the hostnames.** Render service names are globally unique
across all accounts, so `gigbid-api` may already be taken and your service will
get a different hostname. Enter the two secrets now, put your best guess in for
the three URLs, and then **check the real URLs on each service's page once the
services exist** and correct them. Which correction costs what:

| Variable | When it is read | Cost of fixing it later |
| --- | --- | --- |
| `CORS_ORIGINS`, `APP_BASE_URL` | runtime | free: save and the service restarts |
| `NEXT_PUBLIC_API_URL` | **build time** | a rebuild: Manual Deploy -> **Clear build cache & deploy** |

On **`gigbid-api`**:

| Key | Value |
| --- | --- |
| `OPEN_LLM_API_KEY` | your Groq key — the one in `.env`, never committed |
| `REVIEWER_PASSWORD` | the reviewer password; goes in the private Classroom comment |
| `CORS_ORIGINS` | `https://gigbid-web.onrender.com` (no trailing slash) |
| `APP_BASE_URL` | `https://gigbid-api.onrender.com` |

On **`gigbid-web`**:

| Key | Value |
| --- | --- |
| `NEXT_PUBLIC_API_URL` | `https://gigbid-api.onrender.com` |

> **`NEXT_PUBLIC_API_URL` must be set before the first build.** Next.js inlines
> it into the client bundle at build time rather than reading it at runtime.
> Render passes service environment variables to Docker builds as build args
> and `Dockerfile.web` declares the matching `ARG`, so it works — but setting
> it after a build leaves a deployed UI that loads fine and calls
> `http://localhost:8000` for its data. If that happens: set the value, then
> **Manual Deploy → Clear build cache & deploy**.

### 4. Deploy

Apply the blueprint. The API image pre-generates the corpus and warms the
vector index during build, so the first request is not a 110-second embed.
Expect 5–10 minutes for the first build of each service.

### 5. Verify

```bash
curl https://gigbid-api.onrender.com/health
curl https://gigbid-api.onrender.com/gigsim/health   # proves the mount
```

The second should report ~196 jobs and 140 clients. Then open
`https://gigbid-web.onrender.com/login` and sign in as the reviewer. The
account is seeded automatically on boot by `seed_reviewer()` in the API
lifespan — there is no manual seeding step.

---

## What the free plan costs you, honestly

**Retrieval quality does not match the measured results.** A free instance is
512 MB and 0.1 CPU. Torch plus bge-small does not comfortably fit, so
`render.yaml` sets `EMBEDDING_BACKEND=hash`. Every number in
`evals/results/rag_variants.json` and `ablation.json` was measured with
bge-small, and **the deployment does not reproduce them.** The index is built
with the same hash backend at image build time, so queries and corpus stay
consistent and the system behaves correctly — it just retrieves less well than
the evaluation says it can.

Moving `gigbid-api` to a Starter instance and setting `EMBEDDING_BACKEND=local`
restores the measured behaviour. That is the one place where the free plan
changes a result rather than just a convenience, so it should be stated in the
report rather than left for a marker to discover.

**Services sleep after 15 minutes idle.** First request after a sleep takes
~50 seconds per service. With GigSim mounted there are two services in the
path, not four, so a cold open is around 100 seconds rather than 200. Warm it
before recording the demo video or a marker will think it has hung.

**The free Postgres is deleted after 30 days.** Diary it. If grades land later
than that, recreate the database, update the two `fromDatabase` references, and
redeploy; the reviewer account reseeds itself on boot.

**GigSim's world is in memory.** It reseeds deterministically from the same
seed on every restart, so the job list is stable, but submitted proposals and
pipeline history reset when the service sleeps. Connects reset to 120. Fine for
a demo, worth knowing before recording one.

**The scouting worker is off.** `RUN_WORKER_IN_API=0`. Turn it on only
deliberately: a scouting pass analyses every new job on every saved search with
a real model, which on a free LLM tier spends the quota overnight with nobody
watching. The Analyse button and the whole approval path work without it.

---

## If the LLM quota is exhausted

The deployment still runs. `agent/llm.py` falls back to the offline
deterministic provider when the key is missing or rejected, so the UI, the
approval gate and the exactly-once submission path all demonstrate correctly —
the proposal text is placeholder. `GET /system/status` reports which provider
is live, so the state is visible rather than silent.

---

## Alternatives considered

**Single VPS + `docker compose up`.** Runs the exact tested topology with no
rearrangement, no sleep and no 30-day expiry, for about €4/month. The better
choice if a card is available; this document exists because the free plan was
preferred.

**Vercel + Railway split.** Front end always-on and fast, backend paid after
the trial credit, two sets of environment variables to keep in sync.

**Cloudflare Tunnel from a laptop.** Live in five minutes and fine for
recording a video against a real HTTPS URL, but it does not satisfy "the URL
must stay live until grades are released."
