# DevOps File Generator

A Flask web app that takes a public GitHub repository URL, inspects its
full file tree, detects the tech stack, and generates:

- a `Dockerfile`
- a `docker-compose.yml`
- a `Jenkinsfile`

tailored to that stack — including handling dependencies nested in a
subfolder (`app/`, `backend/`, `src/`) rather than the repo root.

## Why this project

Built while learning DevOps fundamentals (Docker, Jenkins, CI/CD).
Instead of practicing on a to-do app, this tool makes me actually
encode the *rules* behind Dockerfiles and Jenkinsfiles — what base
image to pick, how caching layers should be ordered, why Java needs
a multi-stage build and Python doesn't, and why a `pyproject.toml`
project might use `pip`, `poetry`, or `uv` depending on which lockfile
sits next to it.

## What it detects

| Language | Trigger file(s) | Framework / tool detection |
|---|---|---|
| Python | `requirements.txt`, `Pipfile`, `pyproject.toml` | Flask, Django, FastAPI — and for `pyproject.toml`, whether the project uses **uv** (`uv.lock`), **Poetry** (`poetry.lock`), or plain `pip` |
| Node.js | `package.json` | Express, React, Next.js |
| Java | `pom.xml` (Maven) or `build.gradle` (Gradle) | Multi-stage build (compile with JDK, run with JRE) |
| Go | `go.mod` | — |
| Ruby | `Gemfile` | — |

Detection is rule-based, not AI-based, so the logic is fully readable
in `app.py`, no black box.

### Nested-folder detection

Rather than only checking the repo root, it calls GitHub's Git Trees
API (`recursive=1`) to see every file in the repo, then searches for
a manifest anywhere in the tree. If the manifest is nested (e.g.
`app/requirements.txt`), the generated Dockerfile's `COPY` paths are
adjusted automatically, and a banner explains to build from the repo
root, not from inside the subfolder.

## Run it locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`.

## Run it with Docker

```bash
docker build -t devops-generator:1.0 .
docker run -d -p 5000:5000 --name devops-generator devops-generator:1.0
```

## Known limitations

This tool generates a solid **first draft**, not a guaranteed working
config. It intentionally shows warnings rather than pretending to be
certain, because:

- **It can't see runtime behavior.** Required environment variables
  (API keys, admin passwords, secret keys), which host address an app
  binds to (`0.0.0.0` vs `localhost`), and non-default ports are all
  things a real app can enforce at startup that no filename can reveal.
  Always check the repo's README and any `.env.example` before running
  the generated container.
- **If the repo already ships its own Dockerfile, trust that one.**
  It's written and tested by the maintainer for that exact app. The
  generated file is shown as a learning reference, not a replacement.
- Unauthenticated GitHub API calls are rate-limited to 60/hour per IP.
- Generated Jenkinsfile assumes a Docker Hub credential ID named
  `dockerhub-credentials` configured in Jenkins — update this to match
  your own setup.
- Detection is based on file presence and lockfiles, not full static
  analysis, so unusual project layouts may still need manual fixes.

## A real bug I found and fixed

I tested this against [DogukanUrker/FlaskBlog](https://github.com/DogukanUrker/FlaskBlog),
a real Flask app that uses `pyproject.toml` + `uv`, not `requirements.txt` + `pip`.

The first version of this tool treated every Python manifest the same
way, which produced:

```dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r pyproject.toml   # invalid — pip -r doesn't work on pyproject.toml
```

I fixed it by checking for `uv.lock` or `poetry.lock` sitting next to
`pyproject.toml` to determine which tool the project actually uses,
and generating the right install/run commands for each:

| Manifest found | Install command generated | Start command generated |
|---|---|---|
| `requirements.txt` | `pip install -r requirements.txt` | `python app.py` |
| `pyproject.toml` + `uv.lock` | `pip install uv && uv sync --frozen` | `uv run app.py` |
| `pyproject.toml` + `poetry.lock` | `pip install poetry && poetry install --no-root` | `python app.py` |
| `pyproject.toml` alone | `pip install .` | `python app.py` |

I verified the fix produces output matching what I confirmed worked
by hand: `COPY pyproject.toml uv.lock ./`, `uv sync --frozen`, and
`CMD ["uv", "run", "app.py"]`.

## What I learned building this

- How to call the GitHub REST API (contents + recursive Git Trees) and
  handle rate limits gracefully
- Writing a Dockerfile generator that correctly branches between a
  single-stage build (Python/Node/Go/Ruby) and a multi-stage build (Java)
- That `pyproject.toml` alone doesn't tell you which install tool to
  use — the lockfile next to it does
- Why dependency files get copied into the image before the rest of
  the source code (Docker layer caching)
- Structuring a Jenkinsfile with separate checkout, build, test, push,
  and deploy stages, including `dir()` blocks for apps nested in a
  subfolder
- That automated scaffolding tools (this one, and real ones like
  `docker init` or Heroku's buildpacks) are meant to produce a strong
  first draft for a human to review, not a guaranteed, zero-review
  result — debugging the output against a real repo is the actual
  DevOps skill being practiced here
