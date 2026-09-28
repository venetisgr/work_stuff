"""The Fly.io deployment files agree with each other and with the code: fly.toml, the Dockerfile, .dockerignore, the
GitHub Actions workflow and docs/DEPLOY.md.

What matters most: exactly one Machine that never stops (the scanner runs inside the website), the volume at DATA_DIR,
the port the proxy talks to is the one `serve` listens on, the health check path answers 200 over plain HTTP, the
deployment debates with OpenAI's and Anthropic's models (and the image can), and the workflow deploys only from main
(after a push, or by hand) and only when the Fly token exists.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import socket
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dip_scanner import cli
from dip_scanner.config import DATABASE_NAME, ScannerConfig, Settings, WebSettings, load_settings
from dip_scanner.web import server
from dip_scanner.web.app import create_app
from dip_scanner.web.control import STOP_TIMEOUT

PROJECT = Path(__file__).resolve().parent.parent
REPOSITORY = PROJECT.parent
FLY_TOML = PROJECT / "fly.toml"
DOCKERFILE = PROJECT / "Dockerfile"
DOCKERIGNORE = PROJECT / ".dockerignore"
DEPLOY_MD = PROJECT / "docs" / "DEPLOY.md"
WORKFLOW = REPOSITORY / ".github" / "workflows" / "news-dip-scanner.yml"

# Fly.io's regions on 2026-09-28 (https://fly.io/docs/reference/regions/); otp (Bucharest) and the other former
# regions near Greece are gone.
FLY_REGIONS = {
    *("ams", "arn", "cdg", "fra", "lhr"),  # Europe
    *("dfw", "ewr", "iad", "lax", "ord", "sjc", "yyz"),  # North America
    *("gru", "jnb", "nrt", "sin", "syd"),  # South America, Africa, Asia, Oceania
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to use the network: {args[:2]}")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def fly() -> dict:
    return tomllib.loads(FLY_TOML.read_text(encoding="utf-8"))


def seconds(duration: str | int) -> float:
    """A fly.toml duration: "30s", "1m", "500ms", or a bare number of seconds (kill_timeout's old form)."""
    if isinstance(duration, int):
        return float(duration)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", duration)
    assert match, f"not a duration: {duration!r}"
    return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def megabytes(size: str | int) -> int:
    if isinstance(size, int):
        return size
    match = re.fullmatch(r"(?i)(\d+)\s*(mb|gb)", size)
    assert match, f"not a memory size: {size!r}"
    return int(match.group(1)) * (1024 if match.group(2).lower() == "gb" else 1)


# --- Dockerfile ----------------------------------------------------------------------------------------------------


def instructions() -> list[tuple[str, str]]:
    """The Dockerfile's (INSTRUCTION, arguments), with continuation lines joined and comments dropped."""
    joined: list[str] = []
    current = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):  # a comment inside a continued instruction
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        joined.append(current + line)
        current = ""
    assert not current, "the Dockerfile ends in a line continuation"
    result = []
    for line in joined:
        keyword, _, rest = line.partition(" ")
        result.append((keyword.upper(), rest.strip()))
    return result


def last(keyword: str) -> str:
    values = [rest for name, rest in instructions() if name == keyword]
    assert values, f"the Dockerfile has no {keyword}"
    return values[-1]


def docker_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for name, rest in instructions():
        if name == "ENV":
            for item in shlex.split(rest):
                key, _, value = item.partition("=")
                env[key] = value
    return env


def docker_cmd() -> list[str]:
    return json.loads(last("CMD"))  # the exec form, so the process gets Fly's stop signal itself


def test_dockerfile_builds_on_python_3_12_slim_and_runs_as_a_non_root_user():
    assert instructions()[0] == ("FROM", "python:3.12-slim")
    user = last("USER")
    assert user not in ("root", "0") and not user.startswith("0:")
    run = " ".join(rest for name, rest in instructions() if name == "RUN")
    assert re.search(rf"useradd\b.*\b{re.escape(user)}\b", run), "the USER is created in the image"
    assert f"chown {user}:{user} /data" in run, "the user owns the mount point (Fly gives it the image's user)"


def test_dockerfile_command_is_serve_on_the_exposed_port():
    cmd = docker_cmd()
    assert cmd[:2] == ["dip-scanner", "serve"]
    args = cli._parser().parse_args(cmd[1:])
    assert args.handler is cli._serve
    assert args.host == "0.0.0.0"  # reachable by Fly's proxy, not only inside the Machine
    assert not args.no_scanner
    assert last("EXPOSE") == str(args.port)


def test_dockerfile_installs_the_web_extra_with_optional_extras_and_the_settings_files():
    (extras,) = [rest for name, rest in instructions() if name == "ARG" and rest.startswith("EXTRAS")]
    assert extras == 'EXTRAS="anthropic"', "fly.toml's debate needs Anthropic's package by default"
    run = " ".join(rest for name, rest in instructions() if name == "RUN")
    assert "[web${EXTRAS:+,$EXTRAS}]" in run
    assert docker_env()["DATA_DIR"] == "/data"
    assert docker_env()["PYTHONUNBUFFERED"] == "1"  # log lines reach `fly logs` at once
    workdir = last("WORKDIR")
    copies = [rest for name, rest in instructions() if name == "COPY"]
    assert "scanner.toml feeds.toml ./" in copies, "the settings files sit in the working directory"
    assert workdir == "/app"


def test_dockerignore_keeps_secrets_and_data_out_but_not_what_the_dockerfile_copies():
    patterns = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    def ignored(path: str) -> bool:
        verdict = False
        for pattern in patterns:
            negate = pattern.startswith("!")
            body = pattern[1:] if negate else pattern
            body = body.rstrip("/")
            parts = path.split("/")
            prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]  # "data/" also covers data/x
            names = (body, body.removeprefix("**/"))
            if any(fnmatch.fnmatch(prefix, name) for prefix in prefixes for name in names):
                verdict = not negate
        return verdict

    for secret in (".env", ".env.local", "data/scanner.sqlite3", "tests/test_web.py", "dip_scanner/__pycache__/x.pyc"):
        assert ignored(secret), secret
    needed = (
        "Dockerfile",
        "pyproject.toml",
        "README.md",
        "scanner.toml",
        "feeds.toml",
        "dip_scanner/cli.py",
        "dip_scanner/web/templates/base.html",
        "dip_scanner/web/static/app.css",
    )
    for path in needed:
        assert not ignored(path), path
    for name, rest in instructions():
        if name == "COPY":
            for source in shlex.split(rest)[:-1]:
                assert not ignored(source), f"the Dockerfile copies {source}, which .dockerignore leaves out"


# --- fly.toml ------------------------------------------------------------------------------------------------------


def test_fly_toml_keeps_exactly_one_machine_running():
    config = fly()
    service = config["http_service"]
    assert service["auto_stop_machines"] == "off", "the scanner must keep running between visits"
    assert service["auto_start_machines"] is True
    assert service["min_machines_running"] == 1
    assert service["force_https"] is True
    assert "processes" not in config or list(config["processes"]) == ["app"]
    strategy = config.get("deploy", {}).get("strategy", "rolling")
    assert strategy in ("rolling", "immediate"), "canary and bluegreen would start a second Machine"


def test_fly_toml_region_exists_and_is_the_one_the_guide_uses():
    config = fly()
    assert config["primary_region"] in FLY_REGIONS
    guide = DEPLOY_MD.read_text(encoding="utf-8")
    assert f"--region {config['primary_region']}" in guide
    assert f'app = "{config["app"]}"' in guide


def test_fly_toml_mounts_the_volume_at_data_dir():
    config = fly()
    mounts = config["mounts"]
    mounts = mounts[0] if isinstance(mounts, list) else mounts
    assert mounts["destination"] == "/data" == config["env"]["DATA_DIR"] == docker_env()["DATA_DIR"]
    assert 1 <= mounts["snapshot_retention"] <= 60
    guide = DEPLOY_MD.read_text(encoding="utf-8")
    created = re.search(r"fly volumes create (\S+) --region (\S+) --size \d+ --snapshot-retention (\d+)", guide)
    assert created, "the guide creates the volume"
    assert created.group(1) == mounts["source"]
    assert created.group(2) == config["primary_region"]
    assert int(created.group(3)) == mounts["snapshot_retention"]


def test_fly_toml_env_is_not_secret_and_valid():
    env = fly()["env"]
    assert all(not name.startswith("FLY_") for name in env), "Fly reserves FLY_ names"
    assert all(isinstance(value, str) for value in env.values()), "[env] values must be strings"
    secret_words = ("KEY", "TOKEN", "PASSWORD", "SECRET")
    assert not [name for name in env if any(word in name for word in secret_words)], "secrets go in `fly secrets`"
    assert env["DISPLAY_TZ"] == "Europe/Athens"


def test_fly_toml_debates_with_openai_and_anthropic_and_the_guide_sets_both_keys():
    """The owner's deployment: triage with OpenAI's small model, every analysis a debate between OpenAI's and
    Anthropic's models, so both keys are secrets and the image has Anthropic's package."""
    env = fly()["env"]
    settings = load_settings(env)
    assert settings.llm.provider == "openai" and settings.llm.analysis_mode == "debate"
    assert {entry.split(":")[0] for entry in settings.llm.debaters} == {"openai", "anthropic"}
    assert "EXTRAS" not in fly().get("build", {}).get("args", {}), "the Dockerfile's default includes anthropic"
    guide = DEPLOY_MD.read_text(encoding="utf-8")
    secrets = guide[guide.index("fly secrets set \\") :]
    secrets = secrets[: secrets.index("```")]
    assert "OPENAI_API_KEY=" in secrets and "ANTHROPIC_API_KEY=" in secrets
    # Alerts go through each user's own Slack or Discord webhook: no server mail or bot is needed.
    assert "Slack" in guide and "Discord" in guide and "no SMTP" in guide


def test_the_proxy_port_is_the_one_serve_listens_on():
    port = fly()["http_service"]["internal_port"]
    assert port == int(last("EXPOSE"))
    assert docker_cmd()[docker_cmd().index("--port") + 1] == str(port)
    assert port == server.DEFAULT_PORT  # `dip-scanner serve` without --port matches too
    assert cli._parser().parse_args(["serve"]).port == port


def test_fly_waits_long_enough_for_a_clean_stop():
    config = fly()
    assert config["kill_signal"] in ("SIGINT", "SIGTERM")  # uvicorn shuts down gracefully on both
    assert seconds(config["kill_timeout"]) > server.GRACEFUL_SHUTDOWN_SECONDS + STOP_TIMEOUT
    assert seconds(config["kill_timeout"]) <= 300  # Fly's maximum


def test_fly_toml_machine_has_the_measured_memory():
    (vm,) = fly()["vm"]
    assert vm["size"] == "shared-cpu-1x"
    assert megabytes(vm["memory"]) >= 512  # about 170 MB measured; 256 MB leaves too little (docs/DEPLOY.md)


def test_the_health_check_path_answers_200_over_plain_http(tmp_path):
    (check,) = fly()["http_service"]["checks"]
    assert check.get("method", "GET").upper() == "GET"
    assert seconds(check["timeout"]) < seconds(check["interval"])
    assert seconds(check["grace_period"]) >= 10
    settings = Settings(data_dir=tmp_path, web=WebSettings(secret_key="k" * 40, base_url="https://my-dips.fly.dev"))
    app = create_app(settings=settings, config=ScannerConfig(), feeds=[], store_path=tmp_path / DATABASE_NAME)
    # Fly checks the Machine directly, over HTTP on the private network, and doesn't follow redirects.
    client = TestClient(app, base_url="http://[fdaa:0:1::2]:8080", follow_redirects=False)
    response = client.get(check["path"])
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- the GitHub Actions workflow -----------------------------------------------------------------------------------


def workflow_text() -> str:
    if not WORKFLOW.exists():
        if (REPOSITORY / ".git").exists():
            pytest.fail(f"{WORKFLOW} is missing")
        pytest.skip("not inside the work_stuff repository")
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_text_gates_the_deploy():
    text = workflow_text()
    assert "workflow_dispatch:" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}" in text
    assert "flyctl deploy --remote-only" in text
    assert re.search(r"superfly/flyctl-actions/setup-flyctl@[0-9a-f]{40}\b", text), "pin the third-party action"
    assert '"news-dip-scanner/**"' in text and '".github/workflows/news-dip-scanner.yml"' in text


def test_workflow_structure():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(workflow_text())
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    for event in ("pull_request", "push"):
        # The Next.js front end (Vercel deploys it) alone neither tests nor restarts the scanner.
        assert triggers[event]["paths"] == [
            "news-dip-scanner/**",
            "!news-dip-scanner/frontend/**",
            ".github/workflows/news-dip-scanner.yml",
        ]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}

    test = workflow["jobs"]["test"]
    assert test["defaults"]["run"]["working-directory"] == "news-dip-scanner"
    python = next(step for step in test["steps"] if str(step.get("uses", "")).startswith("actions/setup-python"))
    assert str(python["with"]["python-version"]) == "3.12"
    commands = [step.get("run", "") for step in test["steps"]]
    assert 'python -m pip install -e ".[dev,web]"' in commands
    assert "ruff check ." in commands
    assert "ruff format --check ." in commands
    assert any(command.startswith("python -m pytest") for command in commands)

    deploy = workflow["jobs"]["deploy"]
    assert deploy["needs"] == "test"
    condition = " ".join(deploy["if"].split())
    assert condition == (
        "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert deploy["env"]["FLY_API_TOKEN"] == "${{ secrets.FLY_API_TOKEN }}"
    assert deploy["concurrency"]["cancel-in-progress"] is False  # never cut a deploy off halfway
    notice, *steps = deploy["steps"]
    assert notice["if"] == "env.FLY_API_TOKEN == ''"
    assert steps and all(step["if"] == "env.FLY_API_TOKEN != ''" for step in steps), "every step needs the token"
    (run,) = [step for step in steps if "run" in step]
    assert run["working-directory"] == "news-dip-scanner"
    assert run["run"].startswith("flyctl deploy --remote-only")
    assert "--ha=false" in run["run"]


# --- docs/DEPLOY.md ------------------------------------------------------------------------------------------------


def slug(heading: str) -> str:
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[`*_\[\]()]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(line.lstrip("#")) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")}


def test_deploy_guide_links_resolve():
    text = DEPLOY_MD.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        file = (DEPLOY_MD.parent / path).resolve() if path else DEPLOY_MD
        if path.startswith("../../") and not (REPOSITORY / ".git").exists():
            continue  # a file at the repository's root, and this copy isn't in the repository
        assert file.exists(), f"DEPLOY.md links to a missing file: {target}"
        if anchor:
            assert anchor in anchors(file), f"DEPLOY.md links to a missing heading: {target}"


def test_readme_links_and_screenshots_resolve():
    """The README's links to files and headings (its contents line, docs/DEPLOY.md, the workflow) and its screenshots
    exist; the screenshots stay small PNGs."""
    readme = PROJECT / "README.md"
    text = readme.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    assert "docs/DEPLOY.md" in targets and "#deploy-to-flyio" in targets and "#security-model" in targets
    for target in targets:
        if target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        file = (PROJECT / path).resolve() if path else readme
        if path.startswith("../") and not (REPOSITORY / ".git").exists():
            continue  # a file at the repository's root, and this copy isn't in the repository
        assert file.exists(), f"README.md links to a missing file: {target}"
        if anchor:
            assert anchor in anchors(file), f"README.md links to a missing heading: {target}"
    shots = re.findall(r"!\[[^\]]+\]\((docs/screenshots/[^)]+\.png)\)", text)
    assert len(shots) == 3
    for shot in shots:
        data = (PROJECT / shot).read_bytes()
        assert data.startswith(b"\x89PNG") and len(data) < 250_000, shot


def test_deploy_guide_covers_the_one_machine_rule_and_the_admin():
    text = DEPLOY_MD.read_text(encoding="utf-8")
    for command in (
        "fly deploy --ha=false",
        "fly scale count 1",
        'fly ssh console -C "dip-scanner users add-admin you@example.com"',
        "fly tokens create deploy",
        "fly certs add",
        'fly ssh console -C "dip-scanner backup"',
        "fly ssh sftp get /data/backups/",
        "fly volumes snapshots create",
    ):
        assert command in text, command
