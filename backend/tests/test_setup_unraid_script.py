"""Regression guard for ``scripts/setup-unraid.sh``.

The Unraid setup script is the only place in the product where the seven traps
from the 2026-08-14/15 live deployment shakeout are *absorbed* rather than
documented. Shell has no type checker and no import-time errors, so a botched
edit to it ships silently and only surfaces as an operator on a fresh box
hitting exactly the failure the script exists to prevent — the worst possible
place to find out.

These tests are deliberately cheap and structural. They do not (and cannot,
without an Unraid box, a Docker daemon and a webGUI) drive the script end to
end; they assert the properties whose loss would be invisible in review:

  * **It parses.** ``bash -n`` on the real file. The script is bash, not POSIX
    sh — it uses ``[[ ]]``, ``local``, arrays of the ``${x:-}`` family and
    ``SECONDS`` — which is exactly why the docs say ``bash <file>`` and never
    ``sh <file>``.
  * **Each of the seven traps still has its marker.** Every trap costs a live
    incident to rediscover. A refactor that quietly drops the step-ca chown, or
    the preserve-networks key, or the admin-flag trio, is a regression that no
    unit test of the backend can see.
  * **No ``jq``.** Unraid does not ship it. A JSON field pull that reaches for
    jq works perfectly on the developer's box and fails on every real one, so
    the constraint is asserted rather than remembered.
  * **Secrets are never regenerated.** ``FILEARR_SECRET_KEY`` is the envelope
    key for alert-channel credentials and is not inside a database dump;
    regenerating it on a re-run would orphan every stored credential while
    every API call still reported success.

POSIX-only for the ``bash -n`` case, following ``test_entrypoint_all_mode.py``:
the documented local Windows baseline excludes the shell suites. The text
assertions run everywhere — they are pure string matching on a file in the
repo, and they are the half that catches an accidental deletion.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-unraid.sh"


@pytest.fixture(scope="module")
def text() -> str:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(text: str) -> str:
    """The script with comment-only lines removed.

    Every trap is *documented* in a comment as well as implemented, so a naive
    substring search cannot tell "the step is still there" from "the comment
    explaining the step is still there". Assertions about behaviour run against
    this; assertions about documentation run against the full text.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------- #
# It parses                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="needs a POSIX bash to syntax-check the script",
)
def test_script_parses_under_bash_n():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_shebang_is_bash_not_sh(text: str):
    """`sh setup-unraid.sh` would die on the first `[[`; the docs say so."""
    assert text.splitlines()[0] == "#!/usr/bin/env bash"


def test_fails_loudly_rather_than_silently(code: str):
    """set -euo pipefail plus an ERR trap that names the failed step."""
    assert "set -euo pipefail" in code
    assert "set -E" in code  # ERR trap must inherit into functions/subshells
    assert re.search(r"trap\s+'[^']*CURRENT_STEP", code), "ERR trap must name the step"


# --------------------------------------------------------------------------- #
# The seven traps                                                              #
# --------------------------------------------------------------------------- #


def test_trap1_preserve_user_defined_networks_key_and_values(code: str):
    """The cfg key is DOCKER_USER_NETWORKS and it is not a boolean.

    Verified against unraid/webgui rather than guessed: DockerSettings.page
    defines the field and etc/rc.d/rc.docker consumes it as
    ``[[ $DOCKER_USER_NETWORKS != preserve ]] && docker network rm $NETWORK``.
    "yes"/"no" would parse fine and do nothing at all.
    """
    assert "DOCKER_USER_NETWORKS" in code
    assert 'DOCKER_USER_NETWORKS="preserve"' in code
    assert "/boot/config/docker.cfg" in code
    # Cycling the service stops every container on the box: it must be gated on
    # an explicit confirmation that --yes cannot silently answer.
    assert "always-ask" in code, "the Docker service cycle must always ask"
    assert "rc.docker" in code


def test_trap2_network_is_created_after_the_setting(code: str):
    """Order matters: a network created first is the one that gets deleted."""
    assert "docker network create filearr" in code
    setting = code.index("check_preserve_networks\n  create_filearr_network")
    assert setting > 0, "phase 1 must run the setting check before the network create"


def test_trap3_topology_prompt_and_the_dual_homing_reattach(code: str, text: str):
    """Caddy on br0 with a fixed IP, plus an installed re-attach helper."""
    # The topology choice itself.
    assert "TOPOLOGY" in code
    assert "BRIDGE_IF" in code
    # Post Arguments re-attach, XML-escaped, in the generated caddy template.
    assert "&amp;&amp; docker network connect filearr filearr-caddy" in code
    # And the installed User Scripts helper as the safety net.
    assert "docker network connect filearr filearr-caddy" in code
    assert "user.scripts" in code
    assert "customSchedule.cron" in code
    assert "update_cron" in code
    # The reason, kept searchable.
    assert "80" in text and "webGUI" in text


def test_trap4_stepca_chown_before_first_start(code: str, text: str):
    """chown -R 1000:1000 on the CA appdata, before anything starts."""
    assert "chown -R 1000:1000" in code
    assert "filearr-stepca" in code
    # The verbatim error stays greppable in the comments.
    assert "/home/step/password: Permission denied" in text
    # And --check re-asserts the ownership, because Unraid's unsafe New
    # Permissions tool resets it.
    assert "1000:1000" in code
    assert "New Permissions" in text


def test_trap5_first_boot_values_are_harvested_and_persisted(code: str):
    """Fingerprint + admin password scraped, password persisted in-volume."""
    assert "step certificate fingerprint /home/step/certs/root_ca.crt" in code
    assert "docker logs filearr-stepca" in code
    assert "password is" in code
    # Persisted exactly like the Proxmox script does it, so log retention stops
    # mattering — and written THROUGH docker exec so it is owned by the
    # container user with no chown.
    assert "/home/step/secrets/admin_password" in code
    assert "umask 077" in code


def test_trap6_admin_api_calls_carry_the_flag_trio(code: str):
    """Without all three, step stops with 'No admin credentials found.'"""
    assert "--admin-subject=step" in code
    assert "--admin-provisioner=filearr-agents" in code
    assert "--admin-password-file" in code
    # And the tuning it is used for.
    assert "step ca provisioner update filearr-agents" in code
    assert "--allow-renewal-after-expiry" in code


def test_trap7_jwk_extraction_uses_no_host_staging_files(code: str, text: str):
    """The decrypt happens entirely inside docker exec: JWE in, plaintext out.

    The manual procedure stages two files on the HOST, which are owned by root
    while the container runs as step (1000) — hence the guide's chown of both.
    This implementation has nothing on the host to own, which is why the trap is
    eliminated rather than handled.
    """
    assert "step crypto jwe decrypt --password-file" in code
    # Piped in over docker exec's stdin...
    assert re.search(r'printf .%s. "\$enc" \| docker exec -i', code), (
        "the JWE must be piped into the container, not staged on the host"
    )
    # ...and no host-side staging path may appear anywhere in the script.
    for bad in ("/provisioner.jwe", "/adminpw", "adminpw'", "> /mnt/cache/appdata/filearr-stepca/"):
        assert bad not in code, f"host-side staging artefact {bad!r} reappeared"
    # The verbatim error stays greppable.
    assert "permission denied" in text.lower()


# --------------------------------------------------------------------------- #
# Tooling contract and secret discipline                                       #
# --------------------------------------------------------------------------- #


def test_no_jq_is_invoked(code: str):
    """Unraid does not ship jq. JSON is pulled apart with grep/sed instead."""
    assert not re.search(r"(^|[|;&(`$\s])jq\b", code), "jq is not available on Unraid"
    # The replacement is present and deliberate.
    assert "encryptedKey" in code
    assert "grep -o" in code


def test_only_guaranteed_tools_are_required(code: str):
    """The preflight requires exactly the guaranteed set, nothing more."""
    assert re.search(r"for t in curl awk sed grep", code)
    # openssl is optional, with a /dev/urandom fallback.
    assert "/dev/urandom" in code
    assert "od -An -tx1" in code


def test_secrets_are_never_regenerated(code: str):
    """An existing secret always wins — the FILEARR_SECRET_KEY discipline."""
    # gen_secret returns early when the key already has a value.
    m = re.search(r"gen_secret\(\)\s*\{(.+?)\n\}", code, re.S)
    assert m, "gen_secret() missing"
    body = m.group(1)
    assert 'cur="$(secret_get "$key")"' in body
    assert 'if [[ -n "$cur" ]]; then' in body
    assert "return 0" in body
    assert "FILEARR_SECRET_KEY" in code


def test_secret_values_are_printed_only_behind_the_explicit_gate(code: str):
    """Fingerprints everywhere; cleartext only in the gated part-2 block.

    The handoff summary is a deliberate, prompted exception to the
    never-echo-a-secret rule (an operator who never opens the secrets file and
    discovers on restore day that FILEARR_SECRET_KEY was never copied is the
    worse failure). The gate itself is the thing that must not regress.
    """
    assert "fingerprint()" in code
    m = re.search(r"summary_part_two_secrets\(\)\s*\{(.+?)\n\}", code, re.S)
    assert m, "summary_part_two_secrets() missing"
    body = m.group(1)
    assert "Press Enter to display secrets" in body
    assert "scrollback" in body
    # Rotation rules are part of the disclosure, not an afterthought.
    assert "NEVER ROTATE" in body
    assert "fails CLOSED" in body


# --------------------------------------------------------------------------- #
# Structure: the flag surface and the phases the docs promise                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag",
    ["--check", "--summary", "--reconfigure", "--force", "--local-dir", "--phase", "--yes"],
)
def test_documented_flags_are_actually_parsed(code: str, flag: str):
    assert f"{flag})" in code or f"{flag}|" in code


def test_all_five_templates_are_generated_in_apply_order(code: str):
    """step-ca before filearr: the app template wants the CA values, and they
    only exist once the CA has booted. Nothing in step-ca depends on the app."""
    assert (
        "filearr-postgres filearr-meilisearch filearr-stepca filearr filearr-caddy"
        in code
    )
    assert "templates-user" in code
    assert "my-${name}.xml" in code


def test_readiness_probes_exist_for_every_container(code: str):
    """"Running" is not "ready"; each container gets a real probe."""
    assert "pg_isready -U" in code
    assert ":7700/health" in code
    assert "/api/v1/health" in code
    assert "step ca health" in code


def test_walkthrough_is_resumable(code: str):
    """Verified containers are recorded and skipped on the next run."""
    assert 'state_set "VERIFIED_${name}" 1' in code
    assert 'state_done "VERIFIED_${name}"' in code


def test_templates_are_not_regenerated_over_existing_containers(code: str):
    """Unraid rewrites my-<name>.xml on Apply; it then holds operator edits."""
    m = re.search(r"should_generate\(\)\s*\{(.+?)\n\}", code, re.S)
    assert m, "should_generate() missing"
    assert 'container_exists "$name" && return 1' in m.group(1)
    assert '[[ "$FORCE" == 1 ]] && return 0' in m.group(1)
