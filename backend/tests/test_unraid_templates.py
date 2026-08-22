"""UR-T3 — the shipped Unraid Community-Applications templates must be valid.

These XML files are a SHIPPED ASSET: an operator downloads them raw from GitHub
and drops them into `/boot/config/plugins/dockerMan/templates-user/`. Nothing
between this repo and that directory parses them, so a stray `&` or an unescaped
`<` in a Description silently produces a template Unraid refuses to load — with
an error message that names XML, not Filearr. The repo already validates other
shipped assets this way (`test_dsl_help_examples.py` keeps the front-end query
help honest against the real parser), and this is the same idea applied to the
templates.

Beyond well-formedness the checks below encode the conventions that make the
five templates a coherent SET, because those are what actually drift:

  * `Container version="2"` — the CA format Unraid expects.
  * Every secret-bearing field carries `Mask="true"`. A masked field is the only
    thing standing between a screenshot of the Docker tab and a leaked
    Meilisearch master key or CA provisioner JWK.
  * Database-backed containers keep their data on the DIRECT pool path
    (`/mnt/cache/appdata/...`), never `/mnt/user/...`: the shfs FUSE layer has
    unreliable file locking/mmap, the classic Unraid cause of database
    corruption.
  * The merged app+worker container (UR-T2, 2026-08-12) actually asks for merged
    mode, and its stop timeout is long enough for the in-container grace period
    to mean anything — Docker's 10s default kills the container before the
    worker can finish an in-flight job.
  * `unraid/filearr-worker.xml` stays DELETED. It was folded into `filearr.xml`;
    a resurrected copy would reintroduce the two-containers-must-match-by-hand
    drift the merge removed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

UNRAID_DIR = Path(__file__).resolve().parents[2] / "unraid"

# The templates that must exist, and nothing else. Listed explicitly rather than
# globbed so both ADDING and REMOVING one is a deliberate edit to this test.
EXPECTED_TEMPLATES = {
    "filearr.xml",
    "filearr-postgres.xml",
    "filearr-meilisearch.xml",
    "filearr-caddy.xml",
    "filearr-stepca.xml",
    "filearr-agent.xml",
}

# Fields whose VALUE is a credential. Matched on the env-var/target name so a
# renamed <Config Name=...> label cannot quietly drop the mask.
SECRET_TARGETS = {
    "MEILI_MASTER_KEY",
    "FILEARR_MEILI_MASTER_KEY",
    "POSTGRES_PASSWORD",
    "FILEARR_SECRET_KEY",
    "FILEARR_PROXY_SHARED_SECRET",
    "FILEARR_CA_PROVISIONER_JWK",
    "CLOUDFLARE_API_TOKEN",
    "FILEARR_AGENT_TOKEN",
    "DOCKER_STEPCA_INIT_PASSWORD",
}

# Containers whose data path must NOT go through the /mnt/user FUSE layer.
FUSE_SENSITIVE = {
    "filearr-postgres",
    "filearr-meilisearch",
    "filearr-stepca",
    "filearr-agent",
}


def _templates() -> list[Path]:
    return sorted(UNRAID_DIR.glob("*.xml"))


def test_template_set_is_exactly_the_documented_five_plus_agent():
    assert {p.name for p in _templates()} == EXPECTED_TEMPLATES


def test_worker_template_stays_deleted():
    """Folded into filearr.xml by UR-T2; see unraid/README.md's upgrade note."""
    assert not (UNRAID_DIR / "filearr-worker.xml").exists()


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_template_is_well_formed_xml(path: Path):
    ET.parse(path)


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_template_is_ca_container_v2_with_the_required_elements(path: Path):
    root = ET.parse(path).getroot()
    assert root.tag == "Container"
    assert root.get("version") == "2"
    for element in ("Name", "Repository", "Registry", "Overview", "Category"):
        node = root.find(element)
        assert node is not None and (node.text or "").strip(), (
            f"{path.name}: <{element}> missing or empty"
        )
    # The file name and the container name have to agree: Unraid keys its saved
    # my-<name>.xml off <Name>, and a mismatch produces two templates claiming
    # one container (the shadowing trap documented in unraid/README.md).
    assert root.findtext("Name") == path.stem


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_template_urls_point_at_this_file(path: Path):
    """A wrong TemplateURL makes Unraid's update check fetch someone else."""
    url = ET.parse(path).getroot().findtext("TemplateURL")
    assert url is not None and url.endswith(f"/unraid/{path.name}"), url


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_secret_fields_are_masked(path: Path):
    root = ET.parse(path).getroot()
    for cfg in root.findall("Config"):
        if cfg.get("Target") in SECRET_TARGETS:
            assert cfg.get("Mask") == "true", (
                f"{path.name}: {cfg.get('Target')} is a secret but Mask="
                f"{cfg.get('Mask')!r}"
            )


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_config_entries_are_structurally_complete(path: Path):
    """Unraid silently drops a Config missing Type/Target/Mode."""
    root = ET.parse(path).getroot()
    for cfg in root.findall("Config"):
        for attr in ("Name", "Target", "Type", "Display", "Required", "Mask"):
            assert cfg.get(attr) is not None, (
                f"{path.name}: <Config Name={cfg.get('Name')!r}> lacks {attr}"
            )
        assert cfg.get("Type") in {"Path", "Variable", "Port", "Device", "Label"}


@pytest.mark.parametrize(
    "path", [p for p in _templates() if p.stem in FUSE_SENSITIVE], ids=lambda p: p.name
)
def test_database_data_paths_bypass_the_user_share_fuse_layer(path: Path):
    """Read-write data paths on these containers must use /mnt/cache/appdata."""
    root = ET.parse(path).getroot()
    for cfg in root.findall("Config"):
        if cfg.get("Type") != "Path" or cfg.get("Mode") != "rw":
            continue
        default = cfg.get("Default") or ""
        assert not default.startswith("/mnt/user/"), (
            f"{path.name}: {cfg.get('Name')!r} defaults to {default} — SQLite/"
            "LMDB/Postgres through the /mnt/user shfs layer corrupts"
        )


def test_merged_app_container_requests_merged_mode_and_can_finish_its_jobs():
    root = ET.parse(UNRAID_DIR / "filearr.xml").getroot()

    # PostArgs becomes the container command, which the entrypoint reads as
    # argv[1]; `all` is what selects the app+worker supervisor.
    assert (root.findtext("PostArgs") or "").split() == ["all"]

    # Docker SIGKILLs at 10s by default, which is shorter than the in-container
    # grace and would reintroduce the mid-transaction-kill regression that
    # docker-compose.yml's `stop_grace_period: 60s` exists to fix.
    extra = root.findtext("ExtraParams") or ""
    assert "--stop-timeout=60" in extra

    targets = {c.get("Target") for c in root.findall("Config")}
    # The worker knobs the deleted filearr-worker.xml used to own.
    assert {
        "FILEARR_WORKER_CONCURRENCY",
        "FILEARR_WORKER_QUEUES",
        "FILEARR_STOP_GRACE_SECONDS",
    } <= targets
    # ... and the mtls-header parity set, which had no template home before.
    assert {
        "FILEARR_AGENT_AUTH_MODE",
        "FILEARR_PROXY_SHARED_SECRET",
        "FILEARR_CA_URL",
        "FILEARR_CA_FINGERPRINT",
        "FILEARR_CA_PROVISIONER",
        "FILEARR_CA_PROVISIONER_JWK",
    } <= targets


def test_grace_default_does_not_exceed_the_container_stop_timeout():
    """A grace longer than the stop timeout is a grace that never happens."""
    root = ET.parse(UNRAID_DIR / "filearr.xml").getroot()
    grace = next(
        c for c in root.findall("Config")
        if c.get("Target") == "FILEARR_STOP_GRACE_SECONDS"
    )
    extra = root.findtext("ExtraParams") or ""
    timeout = int(extra.split("--stop-timeout=")[1].split()[0])
    assert int(grace.get("Default")) <= timeout


def test_caddy_template_matches_the_published_image_and_its_profiles():
    root = ET.parse(UNRAID_DIR / "filearr-caddy.xml").getroot()
    # UR-T1: this is the tag build.yml's caddy-image job publishes.
    assert root.findtext("Repository").startswith("ghcr.io/filearr/filearr-caddy")

    cfgs = {c.get("Target"): c for c in root.findall("Config")}
    # The profile selector the image's CMD reads.
    assert cfgs["FILEARR_CADDY_PROFILE"].get("Default") == "internal"
    # The upstreams have to name the UNRAID container names, not compose's.
    assert cfgs["FILEARR_APP_UPSTREAM"].get("Default") == "filearr:8000"
    assert cfgs["FILEARR_CA_UPSTREAM"].get("Default") == "filearr-stepca:9000"
    # /step-root is where Caddyfile.acme reads the client-cert trust pool from.
    assert any(
        c.get("Target") == "/step-root" and c.get("Mode") == "ro"
        for c in root.findall("Config")
    )


def test_stepca_and_app_agree_on_the_provisioner_name():
    """A mismatch here mints tokens against a provisioner that does not exist."""
    app = ET.parse(UNRAID_DIR / "filearr.xml").getroot()
    ca = ET.parse(UNRAID_DIR / "filearr-stepca.xml").getroot()

    def default(root, target):
        return next(
            c.get("Default") for c in root.findall("Config")
            if c.get("Target") == target
        )

    assert default(app, "FILEARR_CA_PROVISIONER") == default(
        ca, "DOCKER_STEPCA_INIT_PROVISIONER_NAME"
    )


def test_shared_secret_is_declared_on_both_ends_of_the_proxy_trust():
    """mtls-header compares these byte-for-byte; both templates must offer it."""
    for name in ("filearr.xml", "filearr-caddy.xml"):
        root = ET.parse(UNRAID_DIR / name).getroot()
        assert any(
            c.get("Target") == "FILEARR_PROXY_SHARED_SECRET"
            and c.get("Mask") == "true"
            for c in root.findall("Config")
        ), name
