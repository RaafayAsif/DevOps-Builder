"""
DevOps File Generator
----------------------
Give it a public GitHub repo URL. It inspects the repo's root files,
guesses the tech stack, and generates a Dockerfile, docker-compose.yml,
and Jenkinsfile for it.

Built as a learning project while studying DevOps — the point isn't
that detection is perfect, it's that YOU understand why each generated
line exists.
"""

import os
import re
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

GITHUB_API = "https://api.github.com/repos"


# ---------------------------------------------------------------------
# Step 1: Talk to the GitHub API to see what files are in the repo root
# ---------------------------------------------------------------------
def parse_repo_url(url):
    """
    Turns https://github.com/owner/repo (with or without trailing
    slash, .git, etc.) into ("owner", "repo").
    """
    url = url.strip().rstrip("/")
    url = re.sub(r"\.git$", "", url)
    match = re.search(r"github\.com/([^/]+)/([^/]+)", url)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def get_repo_root_files(owner, repo):
    """
    Calls the GitHub API for the repo's root directory listing.
    No auth token needed for public repos, but GitHub rate-limits
    unauthenticated requests to 60/hour per IP.
    """
    api_url = f"{GITHUB_API}/{owner}/{repo}/contents/"
    resp = requests.get(api_url, timeout=10)
    if resp.status_code != 200:
        return None
    files = [item["name"] for item in resp.json() if item["type"] == "file"]
    return files


def get_file_content(owner, repo, filename):
    """Fetches the raw content of one file, used to peek inside package.json etc."""
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{filename}"
    resp = requests.get(raw_url, timeout=10)
    if resp.status_code == 200:
        return resp.text
    return ""


MANIFEST_FILES = {
    "requirements.txt", "Pipfile", "pyproject.toml",
    "package.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "go.mod",
    "Gemfile",
}


def get_repo_tree(owner, repo):
    """
    Fetches EVERY file path in the repo in one call, using Git's Trees
    API with recursive=1 — this is what lets us find a manifest file
    that's nested inside a subfolder like app/ or backend/, not just
    sitting at the root.
    """
    repo_resp = requests.get(f"{GITHUB_API}/{owner}/{repo}", timeout=10)
    if repo_resp.status_code != 200:
        return None
    default_branch = repo_resp.json().get("default_branch", "main")

    tree_resp = requests.get(
        f"{GITHUB_API}/{owner}/{repo}/git/trees/{default_branch}?recursive=1",
        timeout=10,
    )
    if tree_resp.status_code != 200:
        return None

    tree_json = tree_resp.json()
    paths = [item["path"] for item in tree_json.get("tree", []) if item["type"] == "blob"]
    return paths


def find_manifest(paths):
    """
    Searches the full file tree for a known dependency manifest,
    anywhere in the repo, not just the root. If several exist (e.g.
    one in the real app and one in a test fixture), the SHALLOWEST
    one wins — it's almost always the real application, since test
    fixtures and example projects tend to be nested deeper.

    Returns (filename, subdir, full_path) — subdir is "" if the
    manifest is at the repo root.
    """
    candidates = []
    for path in paths:
        parts = path.split("/")
        filename = parts[-1]
        if filename in MANIFEST_FILES:
            depth = len(parts) - 1
            candidates.append((depth, path, filename))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda c: c[0])
    _, full_path, filename = candidates[0]
    subdir = "/".join(full_path.split("/")[:-1])
    return filename, subdir, full_path


# ---------------------------------------------------------------------
# Step 2: Detect the stack from the root files present
# ---------------------------------------------------------------------
def detect_stack(owner, repo, files, tree_paths=None):
    """
    Returns a dict describing the detected stack. This is rule-based,
    not AI-based — deliberately, so you can read the logic and know
    exactly why it decided what it decided.

    `files` = root-level file names only (used for root badges/display).
    `tree_paths` = every file path in the whole repo, if available —
    used to find a manifest nested inside a subfolder like app/ or
    backend/, when nothing sits at the root.
    """
    info = {
        "language": "unknown",
        "framework": "unknown",
        "base_image": "ubuntu:24.04",
        "install_cmd": "echo no-install-command-detected-edit-this-dockerfile",
        "start_cmd": "echo no-start-command-detected-edit-this-dockerfile",
        "port": 8080,
        "build_tool": None,
        "subdir": "",       # "" means the manifest was at repo root
        "manifest_depth": None,
    }

    # Work out which manifest file to key off, and where it lives.
    manifest_filename, subdir, manifest_path = None, "", None
    if tree_paths:
        manifest_filename, subdir, manifest_path = find_manifest(tree_paths)
    if manifest_filename is None:
        # Fall back to root-only check if the tree API wasn't available
        for candidate in MANIFEST_FILES:
            if candidate in files:
                manifest_filename, subdir, manifest_path = candidate, "", candidate
                break

    if manifest_filename is None:
        info["has_dockerfile_already"] = "Dockerfile" in files
        return info

    info["subdir"] = subdir
    nested = bool(subdir)

    def read_manifest():
        return get_file_content(owner, repo, manifest_path).lower() if manifest_path else ""

    if manifest_filename in ("requirements.txt", "Pipfile", "pyproject.toml"):
        info["language"] = "python"
        info["base_image"] = "python:3.12-slim"
        info["port"] = 5000

        prefix = (subdir + "/") if subdir else ""
        depth = len(subdir.split("/")) if subdir else 0
        sibling_files = [
            p.split("/")[-1] for p in (tree_paths or [])
            if p.startswith(prefix) and p.count("/") == depth
        ]

        # --- Figure out the actual install command for this manifest ---
        # pyproject.toml alone is ambiguous: it could mean uv, poetry, or
        # plain pip. We disambiguate by checking for the matching lockfile
        # sitting next to it, since that's what actually tells you which
        # tool the project uses.
        uses_uv = False
        if manifest_filename == "requirements.txt":
            info["install_cmd"] = "pip install --no-cache-dir -r requirements.txt"
        elif manifest_filename == "Pipfile":
            info["install_cmd"] = "pip install --no-cache-dir pipenv && pipenv install --system --deploy"
        elif manifest_filename == "pyproject.toml":
            if "uv.lock" in sibling_files:
                uses_uv = True
                info["install_cmd"] = "pip install --no-cache-dir uv && uv sync --frozen"
            elif "poetry.lock" in sibling_files:
                info["install_cmd"] = "pip install --no-cache-dir poetry && poetry install --no-root"
            else:
                info["install_cmd"] = "pip install --no-cache-dir ."

        content = read_manifest() if manifest_filename == "requirements.txt" else ""
        # "uv run" executes a .py file directly (it infers the Python
        # call itself), so "uv run app.py" is correct and matches real
        # usage — "uv run python app.py" is redundant and not what
        # projects actually document.
        py_cmd_prefix = "uv run" if uses_uv else "python"

        if "flask" in content or "app.py" in sibling_files:
            info["framework"] = "flask"
            info["start_cmd"] = f"{py_cmd_prefix} app.py"
        elif "django" in content or "manage.py" in sibling_files:
            info["framework"] = "django"
            info["start_cmd"] = f"{py_cmd_prefix} manage.py runserver 0.0.0.0:8000"
            info["port"] = 8000
        elif "fastapi" in content:
            info["framework"] = "fastapi"
            info["start_cmd"] = (
                "uv run uvicorn main:app --host 0.0.0.0 --port 8000" if uses_uv
                else "uvicorn main:app --host 0.0.0.0 --port 8000"
            )
            info["port"] = 8000
        else:
            info["framework"] = "generic-python"
            info["start_cmd"] = f"{py_cmd_prefix} app.py"

        info["manifest_filename"] = manifest_filename
        info["uses_uv"] = uses_uv
        info["lockfile"] = "uv.lock" if uses_uv and "uv.lock" in sibling_files else (
            "poetry.lock" if "poetry.lock" in sibling_files else None
        )

    elif manifest_filename == "package.json":
        info["language"] = "node"
        info["base_image"] = "node:20-alpine"
        info["install_cmd"] = "npm install"
        info["port"] = 3000

        content = read_manifest()
        if "express" in content:
            info["framework"] = "express"
        elif "react" in content:
            info["framework"] = "react"
        elif "next" in content:
            info["framework"] = "next.js"
        else:
            info["framework"] = "generic-node"
        info["start_cmd"] = "npm start"

    elif manifest_filename == "pom.xml":
        info["language"] = "java"
        info["framework"] = "maven"
        info["base_image"] = "eclipse-temurin:17-jdk"
        info["build_tool"] = "maven"
        info["install_cmd"] = "mvn clean package -DskipTests"
        info["start_cmd"] = "java -jar target/*.jar"
        info["port"] = 8080

    elif manifest_filename in ("build.gradle", "build.gradle.kts"):
        info["language"] = "java"
        info["framework"] = "gradle"
        info["base_image"] = "eclipse-temurin:17-jdk"
        info["build_tool"] = "gradle"
        info["install_cmd"] = "gradle build -x test"
        info["start_cmd"] = "java -jar build/libs/*.jar"
        info["port"] = 8080

    elif manifest_filename == "go.mod":
        info["language"] = "go"
        info["base_image"] = "golang:1.22-alpine"
        info["install_cmd"] = "go mod download"
        info["start_cmd"] = "go run ."
        info["port"] = 8080

    elif manifest_filename == "Gemfile":
        info["language"] = "ruby"
        info["base_image"] = "ruby:3.3-slim"
        info["install_cmd"] = "bundle install"
        info["start_cmd"] = "ruby app.rb"
        info["port"] = 4567

    info["has_dockerfile_already"] = "Dockerfile" in files
    info["nested"] = nested
    return info


# ---------------------------------------------------------------------
# Step 3: Generate the three files from the detected stack
# ---------------------------------------------------------------------
def generate_dockerfile(info):
    subdir = info.get("subdir", "")
    # Prefix for COPY source paths — "" at root, "app/" if nested.
    # The Docker build CONTEXT stays the repo root either way; only
    # the COPY source side changes to reach into the subfolder.
    src_prefix = f"{subdir}/" if subdir else ""
    note = (
        f"# NOTE: app code was detected inside '{subdir}/', not the repo root.\n"
        f"# COPY lines below reach into that folder — run `docker build` from\n"
        f"# the REPO ROOT (the folder containing '{subdir}/'), not from inside it.\n\n"
        if subdir else ""
    )

    if info["language"] == "java":
        build_cmd = (
            "mvn clean package -DskipTests"
            if info["build_tool"] == "maven"
            else "gradle build -x test"
        )
        jar_path = "target/*.jar" if info["build_tool"] == "maven" else "build/libs/*.jar"
        return f"""# Auto-generated Dockerfile — multi-stage build
{note}# Stage 1: build the jar with the full JDK + build tool
FROM {info['base_image']} AS build
WORKDIR /app
COPY {src_prefix}. .
RUN {build_cmd}

# Stage 2: run it with just a JRE, much smaller final image
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /app/{jar_path} app.jar
EXPOSE {info['port']}
CMD ["java", "-jar", "app.jar"]
"""

    # Python, Node, Go, Ruby all follow the same basic shape
    if info["language"] == "python":
        py_manifest = info.get("manifest_filename", "requirements.txt")
        py_lockfile = info.get("lockfile")
        if py_lockfile:
            manifest_copy = f"COPY {src_prefix}{py_manifest} {src_prefix}{py_lockfile} ./"
        else:
            manifest_copy = f"COPY {src_prefix}{py_manifest} ."
    else:
        manifest_copy = {
            "node": f"COPY {src_prefix}package*.json ./",
            "go": f"COPY {src_prefix}go.mod {src_prefix}go.sum ./",
            "ruby": f"COPY {src_prefix}Gemfile {src_prefix}Gemfile.lock ./",
        }.get(info["language"], f"COPY {src_prefix}. .")

    return f"""# Auto-generated Dockerfile
{note}FROM {info['base_image']}

WORKDIR /app

# Copy dependency manifest first so Docker can cache this layer
{manifest_copy}
RUN {info['install_cmd']}

# Now copy the rest of the application code
COPY {src_prefix}. .

EXPOSE {info['port']}

CMD {_cmd_to_exec_form(info['start_cmd'])}
"""


def _cmd_to_exec_form(cmd):
    """Turns "python app.py" into ["python", "app.py"] for CMD exec form."""
    parts = cmd.split()
    return "[" + ", ".join(f'"{p}"' for p in parts) + "]"


def generate_compose(info, repo_name):
    return f"""# Auto-generated docker-compose.yml
services:
  {repo_name}:
    build: .
    image: {repo_name}:latest
    ports:
      - "{info['port']}:{info['port']}"
    restart: unless-stopped
    environment:
      - PORT={info['port']}
    # Uncomment if this app needs a database:
    # depends_on:
    #   - db
    # networks:
    #   - app-net

  # db:
  #   image: postgres:16
  #   environment:
  #     POSTGRES_PASSWORD: changeme
  #   volumes:
  #     - db-data:/var/lib/postgresql/data
  #   networks:
  #     - app-net

# volumes:
#   db-data:

# networks:
#   app-net:
"""


def generate_jenkinsfile(info, repo_name, repo_url):
    docker_registry = "your-dockerhub-username"
    return f"""// Auto-generated Jenkinsfile
pipeline {{
    agent any

    environment {{
        IMAGE_NAME = "{docker_registry}/{repo_name}"
        IMAGE_TAG  = "${{BUILD_NUMBER}}"
    }}

    stages {{
        stage('Checkout') {{
            steps {{
                git branch: 'main', url: '{repo_url}'
            }}
        }}

        stage('Install & Test') {{
            agent {{
                docker {{ image '{info["base_image"]}' }}
            }}
            steps {{
                dir('{info.get("subdir") or "."}') {{
                    sh '{info["install_cmd"]}'
                    // Replace with your real test command, e.g. pytest, npm test, mvn test
                    sh 'echo "no tests configured yet"'
                }}
            }}
        }}

        stage('Build Docker Image') {{
            steps {{
                sh 'docker build -t ${{IMAGE_NAME}}:${{IMAGE_TAG}} -t ${{IMAGE_NAME}}:latest .'
            }}
        }}

        stage('Push to Registry') {{
            steps {{
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub-credentials',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {{
                    sh 'echo $DOCKER_PASS | docker login -u $DOCKER_USER --password-stdin'
                    sh 'docker push ${{IMAGE_NAME}}:${{IMAGE_TAG}}'
                    sh 'docker push ${{IMAGE_NAME}}:latest'
                }}
            }}
        }}

        stage('Deploy') {{
            steps {{
                sh 'docker compose pull && docker compose up -d'
            }}
        }}
    }}

    post {{
        success {{ echo 'Pipeline succeeded.' }}
        failure {{ echo 'Pipeline failed — check the stage logs above.' }}
    }}
}}
"""


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    repo_url = request.form.get("repo_url", "")
    owner, repo = parse_repo_url(repo_url)

    if not owner or not repo:
        return render_template("index.html", error="That doesn't look like a valid GitHub repo URL.")

    files = get_repo_root_files(owner, repo)
    if files is None:
        return render_template(
            "index.html",
            error="Couldn't read that repo. It may be private, misspelled, or you hit GitHub's rate limit.",
        )

    # Try to get the FULL file tree so we can find a manifest nested
    # inside a subfolder (app/, backend/, src/...), not just the root.
    # If this fails (private repo quirk, huge repo truncation, rate
    # limit), we still fall back to root-only detection gracefully.
    tree_paths = get_repo_tree(owner, repo)

    info = detect_stack(owner, repo, files, tree_paths)

    dockerfile = generate_dockerfile(info)
    compose = generate_compose(info, repo)
    jenkinsfile = generate_jenkinsfile(info, repo, repo_url)

    warnings = []

    if info.get("has_dockerfile_already"):
        warnings.append(
            "This repo already has its own Dockerfile. That one is written and tested by the "
            "maintainer for this exact app — treat it as the source of truth. Use the generated "
            "file below only as a learning reference to compare against, not as a replacement."
        )

    if info["language"] == "unknown":
        warnings.append(
            "Couldn't recognize this repo's stack anywhere in the file tree "
            "(no requirements.txt, package.json, pom.xml, build.gradle, go.mod, or Gemfile found). "
            "The files below are a generic placeholder — edit install_cmd/start_cmd by hand."
        )
    elif info.get("nested"):
        warnings.append(
            f"Dependency manifest found inside '{info['subdir']}/', not the repo root. "
            f"Build the image from the repo root — the generated Dockerfile already accounts for this."
        )

    warnings.append(
        "This tool can only see file names, not runtime behavior — it has no way to know about "
        "required environment variables (API keys, admin passwords, secret keys), which host "
        "address the app binds to, or non-default ports. Always check the repo's README and any "
        "'.env.example' file before running the generated container."
    )

    return render_template(
        "index.html",
        result=True,
        repo_name=repo,
        detected_files=files,
        info=info,
        warnings=warnings,
        dockerfile=dockerfile,
        compose=compose,
        jenkinsfile=jenkinsfile,
    )


@app.route("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
