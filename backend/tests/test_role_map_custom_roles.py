"""Custom roles as OIDC/LDAP role-map targets (2026-08-19).

The role maps used to validate against the three builtins at parse time, so a
role created under Admin → Roles could never be assigned by an IdP claim. Now
any role NAME parses; existence is checked at login against the role registry
(``roles.pick_highest``) — unknown names are dropped with a warning, never
minted as an empty fail-closed role."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from filearr import ldap_auth, oidc, roles
from filearr.config import Settings


def _row(name, *, scopes, ceiling=(), builtin=False):
    return SimpleNamespace(
        name=name,
        display_name=name,
        description="",
        builtin=builtin,
        scopes=list(scopes),
        ceiling_actions=list(ceiling),
    )


@pytest.fixture
def registry():
    roles.reset_for_tests()
    roles._install(
        [
            _row("admin", scopes=["read", "write", "admin"], builtin=True),
            _row("user", scopes=["read", "write"], ceiling=["read", "write"], builtin=True),
            _row("viewer", scopes=["read"], ceiling=["read"], builtin=True),
            # custom: read + write with a wide ceiling (more than builtin user)
            _row("curator", scopes=["read", "write"], ceiling=["read", "write", "delete"]),
            # custom: read only
            _row("auditor", scopes=["read"], ceiling=["read"]),
            # custom: carries the admin scope => bypass
            _row("superop", scopes=["read", "admin"]),
        ]
    )
    yield
    roles.reset_for_tests()


def test_pick_highest_prefers_bypass_then_scopes_then_ceiling(registry):
    assert roles.pick_highest(["viewer", "curator"]) == "curator"
    assert roles.pick_highest(["curator", "user"]) == "curator"  # wider ceiling
    assert roles.pick_highest(["auditor", "viewer"]) == "viewer"  # builtin tiebreak
    assert roles.pick_highest(["curator", "superop"]) == "superop"  # bypass wins
    assert roles.pick_highest(["superop", "admin"]) == "admin"


def test_pick_highest_drops_unknown_with_warning(registry, caplog):
    log = logging.getLogger("test.rolemap")
    with caplog.at_level(logging.WARNING, logger="test.rolemap"):
        assert roles.pick_highest(["ghost", "auditor"], log=log, source="MAP") == "auditor"
        assert roles.pick_highest(["ghost"], log=log, source="MAP") is None
    assert any("ghost" in r.getMessage() for r in caplog.records)


def test_oidc_role_map_targets_custom_role(registry):
    s = Settings(
        auth_enabled=True,
        oidc_enabled=True,
        oidc_issuer="https://idp.example",
        oidc_client_id="filearr",
        oidc_role_claim="groups",
        oidc_role_map="curators:curator,ghosts:ghost,admins:admin",
        oidc_default_role="viewer",
    )
    p = oidc.OIDCProvider(oidc.OidcConfig.from_settings(s))
    assert p.resolve_role({"groups": ["curators"]}) == "curator"
    # unknown custom role ignored -> default
    assert p.resolve_role({"groups": ["ghosts"]}) == "viewer"
    # highest known wins
    assert p.resolve_role({"groups": ["curators", "admins"]}) == "admin"


def test_ldap_role_map_targets_custom_role(registry):
    s = Settings(
        auth_enabled=True,
        ldap_enabled=True,
        ldap_server="ldap://localhost",
        ldap_user_base="ou=people,dc=ex,dc=com",
        ldap_role_map="cn=curators,dc=ex,dc=com=>curator;cn=ghosts,dc=ex,dc=com=>ghost",
        ldap_default_role="",
    )
    cfg = ldap_auth.LdapConfig.from_settings(s)
    assert ldap_auth.resolve_role(cfg, ("cn=curators,dc=ex,dc=com",)) == "curator"
    assert ldap_auth.resolve_role(cfg, ("cn=ghosts,dc=ex,dc=com",)) is None  # fail-closed


def test_role_map_parsers_accept_slugs_only():
    s = Settings(
        oidc_role_map="a:curator,b:Not Valid,c:ok_role-2",
        ldap_role_map="cn=x=>curator;cn=y=>BAD ROLE",
    )
    assert s.oidc_role_map_parsed == {"a": "curator", "c": "ok_role-2"}
    assert s.ldap_role_map_parsed == {"cn=x": "curator"}
