"""Local accounts, sessions & the auth gate surface (Phase 6, P6-T1).

Endpoints (all under ``/api/v1/auth``):

* ``GET  /status``   — public probe: is auth enabled, do any users exist, what
  mode is the UI in (disabled / bootstrap / enabled). No auth required — the SPA
  calls this before deciding whether to show a login wall.
* ``POST /bootstrap`` — create the FIRST admin, allowed ONLY while zero users
  exist (409 afterwards). The first-run escape hatch so enabling auth never
  locks an operator out.
* ``POST /login``    — username/password → ``Set-Cookie: filearr_session``.
* ``POST /logout``   — revoke the current session + clear the cookie.
* ``GET  /me``       — the current session principal (401 if none).
* ``POST /password`` — self password change (verifies the current password).
* ``GET/POST /users`` + ``PATCH/DELETE /users/{id}`` — admin user management.

The login wall coexists with API keys: nothing here changes Bearer-key behaviour
and none of it engages while ``FILEARR_AUTH_ENABLED=false``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, authx, ratelimit, security
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Principal, User
from filearr.models import Session as SessionRow
from filearr.security import (
    clear_session_cookie,
    require_scope,
    resolve_session_principal,
    set_session_cookie,
)

router = APIRouter()

# Auth-outcome log lines carry the METHOD (local / ldap / oidc) so an operator
# reading the app log can tell which provider actually answered a login —
# mirrored into the audit row's ``details.method`` for the Security audit view.
logger = logging.getLogger("filearr.auth")

# Roles are data since 2026-08-16 (builtin admin/user/viewer + custom); a role
# name is validated against the live registry at the endpoint, not a Literal.
GlobalRole = str


def _validate_role_name(name: str) -> str:
    from filearr import roles as roles_registry

    if not name or name not in {r.name for r in roles_registry.all_roles()}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown role '{name}'")
    return name


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class AuthStatus(BaseModel):
    auth_enabled: bool
    users_exist: bool
    mode: Literal["disabled", "bootstrap", "enabled"]
    # P6-T5: true when OIDC SSO is enabled AND minimally configured — the SPA
    # shows the "Sign in with SSO" button off this flag. Always false when auth is
    # disabled or OIDC is unconfigured (fail-closed).
    oidc_enabled: bool = False
    # P6-T6: true when LDAP is enabled AND minimally configured. The login form is
    # unchanged (same username/password POST); the SPA may show an optional
    # "directory sign-in supported" hint off this flag.
    ldap_enabled: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class BootstrapIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    global_role: GlobalRole = "user"
    email: str | None = None


class UserPatchIn(BaseModel):
    global_role: GlobalRole | None = None
    disabled: bool | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=64)
    # Per-user session-timeout overrides in hours. Omitted = leave alone; the
    # explicit sentinel 0 = clear the override (back to the global setting).
    session_inactivity_hours: float | None = Field(default=None, ge=0, le=24 * 365)
    session_ttl_hours: float | None = Field(default=None, ge=0, le=24 * 365)


class ProfileIn(BaseModel):
    """Self-service profile edit (the account page). Username changes are for
    LOCAL accounts only — a federated username is the IdP's, not ours."""

    username: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=64)


class SessionSettingsOut(BaseModel):
    """The global session timeouts: effective value, where it comes from
    (``env`` | ``global``), and the env defaults for the reset hint."""

    inactivity_hours: float
    ttl_hours: float
    inactivity_source: str
    ttl_source: str
    env_inactivity_hours: float
    env_ttl_hours: float
    min_hours: float
    max_hours: float


class SessionSettingsIn(BaseModel):
    # None = leave alone; 0 = clear the override (back to env).
    inactivity_hours: float | None = Field(default=None, ge=0, le=24 * 365)
    ttl_hours: float | None = Field(default=None, ge=0, le=24 * 365)


class MySessionTimeoutsOut(BaseModel):
    inactivity_hours: float
    ttl_hours: float
    inactivity_source: str  # env | global | user
    ttl_source: str


class PrincipalOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str | None
    global_role: str
    kind: str
    disabled: bool
    # P6-T12/T10: the identity source ('local'|'ldap'|'saml'|'oidc') so the admin
    # UI can badge federated accounts, and 'kind' distinguishes a human user from
    # a (future) service_account row.
    auth_provider: str = "local"
    # Self-service profile + preferences (2026-08-16).
    display_name: str | None = None
    phone: str | None = None
    preferences: dict = Field(default_factory=dict)
    # Per-user session-timeout overrides (None = global setting applies).
    session_inactivity_hours: float | None = None
    session_ttl_hours: float | None = None
    # The coarse API scopes this principal's role grants (read/write/admin) —
    # what the SPA should gate admin surfaces on, since a CUSTOM role may carry
    # the admin scope without being named "admin".
    scopes: list[str] = Field(default_factory=list)
    # 2026-08-23: identity-provider details for the admin Users view (LDAP/OIDC).
    external_issuer: str | None = None
    external_subject: str | None = None
    external_profile: dict | None = None
    last_login_at: datetime | None = None


class LoginOut(BaseModel):
    principal: PrincipalOut
    # Surfaced when credentials were sent over plain http (temporarily allowed):
    # the frontend renders it as a nudge to switch to https://<host>:8443.
    warning: str | None = None


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _principal_out(principal: Principal, user: User) -> PrincipalOut:
    return PrincipalOut(
        id=principal.id,
        username=user.username,
        email=user.email,
        global_role=principal.global_role,
        kind=principal.kind,
        disabled=principal.disabled_at is not None,
        auth_provider=user.auth_provider,
        display_name=user.display_name,
        phone=user.phone,
        preferences=dict(principal.preferences or {}),
        session_inactivity_hours=principal.session_inactivity_hours,
        session_ttl_hours=principal.session_ttl_hours,
        scopes=sorted(authx.scopes_for_role(principal.global_role)),
        external_issuer=user.external_issuer,
        external_subject=user.external_subject,
        external_profile=user.external_profile,
        last_login_at=user.last_login_at,
    )


async def _users_exist(session: AsyncSession) -> bool:
    result = await session.execute(select(func.count()).select_from(User))
    return (result.scalar_one() or 0) > 0


async def _load_user(session: AsyncSession, principal_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.principal_id == principal_id))
    return result.scalar_one_or_none()


async def _ldap_eligible(session: AsyncSession, username: str) -> bool:
    """Whether ``/auth/login`` should fall through to LDAP for this username.

    Local-first ordering: an EXISTING local account (any provider other than
    ldap) blocks the fall-through — a same-named local admin stays local and a
    wrong local password never leaks to the directory. Only an unknown username
    or an already-ldap-sourced account is eligible."""
    normalized = username.strip().lower()
    result = await session.execute(select(User).where(User.username == normalized))
    user = result.scalar_one_or_none()
    return user is None or user.auth_provider == "ldap"


async def current_principal(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Dependency: the authenticated session principal, or 401. Session-only
    (Bearer keys are not yet mapped to principals — that is the ApiKey backfill,
    a later additive pass)."""
    principal = await resolve_session_principal(request, response, session)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return principal


def _https_warning(request: Request) -> str | None:
    from filearr.security import _request_is_https

    if _request_is_https(request):
        return None
    return (
        "Credentials were sent over plain http. Use the TLS endpoint "
        "(https://<host>:8443) so the session cookie is protected in transit."
    )


# --------------------------------------------------------------------------- #
# Public probe                                                                 #
# --------------------------------------------------------------------------- #
@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(session: AsyncSession = Depends(get_session)) -> AuthStatus:
    from filearr import authconfig

    settings = get_settings()
    # GUI config overlays env: the login page must offer SSO/LDAP whenever they
    # are configured EITHER way.
    eff = await authconfig.effective_settings(session)
    exists = await _users_exist(session)
    if not settings.auth_enabled:
        mode: Literal["disabled", "bootstrap", "enabled"] = "disabled"
    elif not exists:
        mode = "bootstrap"
    else:
        mode = "enabled"
    return AuthStatus(
        auth_enabled=settings.auth_enabled,
        users_exist=exists,
        mode=mode,
        oidc_enabled=eff.oidc_is_configured,
        ldap_enabled=eff.ldap_is_configured,
    )


# --------------------------------------------------------------------------- #
# Bootstrap / login / logout / me                                             #
# --------------------------------------------------------------------------- #
@router.post("/auth/bootstrap", response_model=PrincipalOut, status_code=201)
async def bootstrap(
    payload: BootstrapIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    """Create the first admin. Allowed only while zero users exist (409 after).
    Deliberately unauthenticated — it is the first-run escape hatch."""
    # P6-T8: the same brute-force lock gate guards bootstrap (a locked IP cannot
    # hammer it either).
    retry_after = await ratelimit.check_locked(payload.username, ratelimit.client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if await _users_exist(session):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user already exists; bootstrap is closed")
    principal = Principal(kind="user", global_role="admin")
    session.add(principal)
    await session.flush()
    user = User(
        principal_id=principal.id,
        username=payload.username.strip().lower(),
        password_hash=authx.hash_password(payload.password),
        auth_provider="local",
    )
    session.add(user)
    await session.commit()
    await audit.emit(
        audit.BOOTSTRAP,
        request=request,
        principal_id=principal.id,
        username_attempted=payload.username,
    )
    return _principal_out(principal, user)


@router.post("/auth/login", response_model=LoginOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> LoginOut:
    ip = ratelimit.client_ip(request)
    # P6-T8: reject a locked username/IP BEFORE the slow argon2 verify runs. The
    # 429 is byte-identical for an unknown vs a known username (anti-enumeration).
    retry_after = await ratelimit.check_locked(payload.username, ip)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    principal = await authx.authenticate_local(session, payload.username, payload.password)
    ldap_result = None
    provider = "local"
    methods_tried = ["local"]
    ldap_error_reason: str | None = None
    # Local-first, then LDAP fall-through (P6-T6): only when local auth did not
    # succeed AND the username is unknown or ldap-sourced. Same login form; the
    # directory verifies the password via a real bind.
    if principal is None:
        from filearr import authconfig

        eff = await authconfig.effective_settings(session)
        if eff.ldap_is_configured and await _ldap_eligible(session, payload.username):
            from filearr import ldap_auth

            methods_tried.append("ldap")
            try:
                ldap_result = await ldap_auth.authenticate_ldap(
                    session, payload.username, payload.password
                )
            except ldap_auth.LDAPError as exc:
                # Config/transport/role-refusal → generic failure (fail-closed);
                # discard any partial provisioning writes. The reason token is
                # audit/log-only — the client still sees the generic 401.
                await session.rollback()
                ldap_result = None
                ldap_error_reason = exc.reason
            if ldap_result is not None:
                principal = await session.get(Principal, uuid.UUID(ldap_result.principal_id))
                provider = "ldap"
    if principal is None:
        await session.rollback()
        # Counter mutations + audit run in their OWN transactions, so the rollback
        # above never discards them (P6-T8/T9).
        newly_locked = await ratelimit.register_failure(payload.username, ip)
        fail_details: dict = {"methods_attempted": methods_tried}
        if ldap_error_reason is not None:
            fail_details["ldap_error"] = ldap_error_reason
        await audit.emit(
            audit.LOGIN_FAILURE,
            request=request,
            username_attempted=payload.username,
            details=fail_details,
        )
        # No username here: app_logs is read-scope (vs the admin-only audit
        # feed) and a failed "username" is sometimes a mistyped password.
        logger.info(
            "login failed from %s (methods tried: %s%s)",
            ip,
            " -> ".join(methods_tried),
            f"; ldap error: {ldap_error_reason}" if ldap_error_reason else "",
        )
        if newly_locked:
            await audit.emit(
                audit.LOCKOUT, request=request, username_attempted=payload.username
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = await authx.create_session(
        session,
        str(principal.id),
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    # A successful auth clears the username bucket (the IP bucket decays naturally).
    await ratelimit.clear_username(payload.username)
    # An LDAP login may have changed the mapped role or synced groups → the
    # effective grants moved; drop the grant cache (P6-T4).
    if ldap_result is not None and (ldap_result.role_changed or ldap_result.groups_changed):
        from filearr import grant_cache

        grant_cache.bump_generation()
    set_session_cookie(response, request, token.raw)
    user = await _load_user(session, principal.id)
    assert user is not None
    await audit.emit(
        audit.LDAP_LOGIN if provider == "ldap" else audit.LOGIN_SUCCESS,
        request=request,
        principal_id=principal.id,
        username_attempted=payload.username,
        details={"method": provider},
    )
    logger.info(
        "login success: user=%s method=%s ip=%s", user.username, provider, ip
    )
    return LoginOut(principal=_principal_out(principal, user), warning=_https_warning(request))


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Response:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if raw:
        # Resolve the owning principal for the audit WITHOUT rotating the token
        # (a plain hash lookup — revoke_session would otherwise miss a rotated row).
        row = (
            await session.execute(
                select(SessionRow).where(
                    SessionRow.session_hash == authx.hash_session_token(raw)
                )
            )
        ).scalar_one_or_none()
        pid = row.principal_id if row is not None else None
        await authx.revoke_session(session, raw)
        await session.commit()
        if pid is not None:
            await audit.emit(audit.LOGOUT, request=request, principal_id=pid)
    clear_session_cookie(response, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=PrincipalOut)
async def me(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    user = await _load_user(session, principal.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return _principal_out(principal, user)


@router.post("/auth/password", status_code=204)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Self-service password change. Verifies the current password, then kills
    every session for the principal (a password change is a privilege event).
    Rate-limited like login so a wrong-current-password guessing loop locks."""
    user = await _load_user(session, principal.id)
    username = user.username if user is not None else None
    ip = ratelimit.client_ip(request)
    retry_after = await ratelimit.check_locked(username, ip)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if user is None or not authx.verify_password(payload.current_password, user.password_hash):
        newly_locked = await ratelimit.register_failure(username, ip)
        await audit.emit(
            audit.LOGIN_FAILURE,
            request=request,
            principal_id=principal.id,
            username_attempted=username,
        )
        if newly_locked:
            await audit.emit(
                audit.LOCKOUT,
                request=request,
                principal_id=principal.id,
                username_attempted=username,
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    user.password_hash = authx.hash_password(payload.new_password)
    await authx.revoke_all_for_principal(session, str(principal.id))
    await session.commit()
    await ratelimit.clear_username(username)
    await audit.emit(
        audit.PASSWORD_CHANGE,
        request=request,
        principal_id=principal.id,
        username_attempted=username,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _override_hours(v: float) -> int | None:
    from filearr import app_settings

    if v == 0:
        return None
    if not (app_settings.MIN_HOURS <= v <= app_settings.MAX_HOURS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"timeout must be between {app_settings.MIN_HOURS:.3f} and "
            f"{app_settings.MAX_HOURS} hours (0 clears)",
        )
    return int(round(v))


# --------------------------------------------------------------------------- #
# Self-service account: profile, preferences, effective timeouts               #
# --------------------------------------------------------------------------- #
@router.patch("/auth/me", response_model=PrincipalOut)
async def patch_me(
    payload: ProfileIn,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    """Edit your own profile. ``username`` only for LOCAL accounts (federated
    usernames belong to the IdP); it is normalised to lowercase and must stay
    unique. Never touches role/disabled/password (those are the admin patch and
    the password endpoint respectively)."""
    user = await _load_user(session, principal.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changed: dict[str, object] = {}
    if payload.username is not None:
        normalized = payload.username.strip().lower()
        if normalized != user.username:
            if user.auth_provider != "local":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This account signs in through an identity provider; "
                    "its username cannot be changed here",
                )
            clash = (
                await session.execute(select(User).where(User.username == normalized))
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, f"User '{normalized}' already exists")
            changed["username"] = {"from": user.username, "to": normalized}
            user.username = normalized
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None
        changed["display_name"] = user.display_name
    if payload.email is not None:
        user.email = payload.email.strip() or None
        changed["email"] = bool(user.email)
    if payload.phone is not None:
        user.phone = payload.phone.strip() or None
        changed["phone"] = bool(user.phone)
    await session.commit()
    if changed:
        await audit.emit(
            audit.PROFILE_UPDATED,
            request=request,
            principal_id=principal.id,
            details={"target": str(principal.id), "changed": changed},
        )
    return _principal_out(principal, user)


_PREFS_MAX_BYTES = 8192


@router.put("/auth/me/preferences", response_model=PrincipalOut)
async def put_my_preferences(
    payload: dict,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    """Replace your preferences object (theme defaults etc.). Free-form JSON,
    size-capped; the SPA owns the key vocabulary."""
    import json as _json

    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "preferences must be an object")
    if len(_json.dumps(payload)) > _PREFS_MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "preferences too large")
    user = await _load_user(session, principal.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    principal.preferences = payload
    await session.commit()
    return _principal_out(principal, user)


@router.get("/auth/me/session-timeouts", response_model=MySessionTimeoutsOut)
async def my_session_timeouts(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> MySessionTimeoutsOut:
    """The idle/absolute timeouts that apply to YOUR sessions and where each
    comes from (env default, the global runtime setting, or a per-user override)."""
    from filearr import app_settings

    eff = await app_settings.effective_session_timeouts(
        session,
        user_inactivity_hours=principal.session_inactivity_hours,
        user_ttl_hours=principal.session_ttl_hours,
    )
    return MySessionTimeoutsOut(
        inactivity_hours=eff.inactivity_hours,
        ttl_hours=eff.ttl_hours,
        inactivity_source=eff.inactivity_source,
        ttl_source=eff.ttl_source,
    )


# --------------------------------------------------------------------------- #
# Global session-timeout settings (admin, runtime — no restart)               #
# --------------------------------------------------------------------------- #
async def _session_settings_out(session: AsyncSession) -> SessionSettingsOut:
    from filearr import app_settings

    g = await app_settings.global_session_timeouts(session)
    s = get_settings()
    return SessionSettingsOut(
        inactivity_hours=g.inactivity_hours,
        ttl_hours=g.ttl_hours,
        inactivity_source=g.inactivity_source,
        ttl_source=g.ttl_source,
        env_inactivity_hours=float(s.session_inactivity_hours),
        env_ttl_hours=float(s.session_ttl_hours),
        min_hours=app_settings.MIN_HOURS,
        max_hours=app_settings.MAX_HOURS,
    )


@router.get(
    "/auth/settings/session",
    response_model=SessionSettingsOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def get_session_settings(
    session: AsyncSession = Depends(get_session),
) -> SessionSettingsOut:
    return await _session_settings_out(session)


@router.patch(
    "/auth/settings/session",
    response_model=SessionSettingsOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def patch_session_settings(
    payload: SessionSettingsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SessionSettingsOut:
    """Set the deployment-wide idle / absolute session timeouts at runtime.
    Applies to every session on its next request (idle) and to sessions created
    from now on (absolute). 0 clears an override back to the env default."""
    from filearr import app_settings

    actor = audit.actor_id(request)
    changed: dict[str, object] = {}
    for key, v in (
        (app_settings.KEY_SESSION_INACTIVITY_HOURS, payload.inactivity_hours),
        (app_settings.KEY_SESSION_TTL_HOURS, payload.ttl_hours),
    ):
        if v is None:
            continue
        val = None if v == 0 else _override_hours(v)
        await app_settings.set_value(session, key, val, updated_by=actor)
        changed[key] = val
    await session.commit()
    if changed:
        await audit.emit(
            audit.SESSION_SETTINGS_CHANGED,
            request=request,
            principal_id=actor,
            details=changed,
        )
    return await _session_settings_out(session)


# --------------------------------------------------------------------------- #
# Admin user management                                                        #
# --------------------------------------------------------------------------- #
@router.get(
    "/auth/users",
    response_model=list[PrincipalOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_users(session: AsyncSession = Depends(get_session)) -> list[PrincipalOut]:
    result = await session.execute(
        select(Principal, User)
        .join(User, User.principal_id == Principal.id)
        .order_by(User.username)
    )
    return [_principal_out(p, u) for p, u in result.all()]


@router.post(
    "/auth/users",
    response_model=PrincipalOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_user(
    payload: UserCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    normalized = payload.username.strip().lower()
    existing = await session.execute(select(User).where(User.username == normalized))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"User '{normalized}' already exists")
    role_name = _validate_role_name(payload.global_role)
    # 2026-08-20 privilege ceiling: creating a user grants that user's role —
    # the creator must hold at least the scopes the role confers.
    granted = await security.caller_scopes(request, session)
    security.require_grant_ceiling(
        security.expand_scopes(authx.scopes_for_role(role_name)),
        granted,
        f"a user with role {role_name!r}",
    )
    principal = Principal(kind="user", global_role=role_name)
    session.add(principal)
    await session.flush()
    user = User(
        principal_id=principal.id,
        username=normalized,
        email=payload.email,
        password_hash=authx.hash_password(payload.password),
        auth_provider="local",
    )
    session.add(user)
    await session.commit()
    await audit.emit(
        audit.USER_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal.id), "username": normalized, "role": payload.global_role},
    )
    return _principal_out(principal, user)


@router.patch(
    "/auth/users/{principal_id}",
    response_model=PrincipalOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def patch_user(
    principal_id: uuid.UUID,
    payload: UserPatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    principal = await session.get(Principal, principal_id)
    user = await _load_user(session, principal_id)
    if principal is None or user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    privilege_change = False
    role_changed_to: str | None = None
    disabled_changed_to: bool | None = None
    if payload.global_role is not None and payload.global_role != principal.global_role:
        new_role = _validate_role_name(payload.global_role)
        # Same ceiling as create: raising a role grants its scopes.
        granted = await security.caller_scopes(request, session)
        security.require_grant_ceiling(
            security.expand_scopes(authx.scopes_for_role(new_role)),
            granted,
            f"role {new_role!r}",
        )
        principal.global_role = new_role
        role_changed_to = principal.global_role
        privilege_change = True
    if payload.disabled is not None:
        from datetime import UTC, datetime

        new_disabled_at = datetime.now(UTC) if payload.disabled else None
        if (new_disabled_at is not None) != (principal.disabled_at is not None):
            principal.disabled_at = new_disabled_at
            disabled_changed_to = payload.disabled
            privilege_change = True
    if payload.email is not None:
        user.email = payload.email
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None
    if payload.phone is not None:
        user.phone = payload.phone.strip() or None
    # 0 clears an override (NULL = global); anything else must sit in the band.
    if payload.session_inactivity_hours is not None:
        principal.session_inactivity_hours = _override_hours(payload.session_inactivity_hours)
    if payload.session_ttl_hours is not None:
        principal.session_ttl_hours = _override_hours(payload.session_ttl_hours)
    if payload.password is not None:
        user.password_hash = authx.hash_password(payload.password)
        privilege_change = True
    # A change of authority (role/disable/password) revokes existing sessions so
    # it takes effect immediately (instant-revocation, research §1.3).
    if privilege_change:
        await authx.revoke_all_for_principal(session, str(principal_id))
    await session.commit()
    if privilege_change:
        # A role change alters the principal's effective grants/ceiling — drop the
        # cached grant set so no stale decision survives (P6-T4).
        from filearr import grant_cache

        grant_cache.bump_generation()
    actor = audit.actor_id(request)
    if role_changed_to is not None:
        await audit.emit(
            audit.ROLE_CHANGED,
            request=request,
            principal_id=actor,
            details={"target": str(principal_id), "role": role_changed_to},
        )
    if disabled_changed_to is not None:
        await audit.emit(
            audit.USER_DISABLED if disabled_changed_to else audit.USER_ENABLED,
            request=request,
            principal_id=actor,
            details={"target": str(principal_id)},
        )
    return _principal_out(principal, user)


@router.delete(
    "/auth/users/{principal_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_user(
    principal_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    principal = await session.get(Principal, principal_id)
    if principal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # Refuse to delete the last remaining admin so auth can never be locked out.
    if principal.global_role == "admin":
        remaining = await session.execute(
            select(func.count())
            .select_from(Principal)
            .where(Principal.global_role == "admin", Principal.id != principal_id)
        )
        if (remaining.scalar_one() or 0) == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Cannot delete the last admin account"
            )
    await session.delete(principal)  # CASCADE removes the user row + sessions
    await session.commit()
    from filearr import grant_cache

    grant_cache.bump_generation()  # principal's grants/memberships gone (P6-T4)
    await audit.emit(
        audit.USER_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal_id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Session management (P6-T11) — "active sessions" + remote logout             #
# --------------------------------------------------------------------------- #
class SessionOut(BaseModel):
    id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    # True for the caller's OWN current session (own-list only) so the UI can
    # label it and refuse a footgun self-revoke without warning.
    current: bool = False


def _session_out(row: SessionRow, current_id: str | None) -> SessionOut:
    return SessionOut(
        id=row.id,
        ip_address=str(row.ip_address) if row.ip_address is not None else None,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        current=current_id is not None and str(row.id) == current_id,
    )


async def _sessions_for(session: AsyncSession, principal_id: uuid.UUID) -> list[SessionRow]:
    result = await session.execute(
        select(SessionRow)
        .where(SessionRow.principal_id == principal_id)
        .order_by(SessionRow.last_seen_at.desc())
    )
    return list(result.scalars().all())


@router.get("/auth/sessions", response_model=list[SessionOut])
async def list_my_sessions(
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    """The caller's own active sessions (IP / user-agent / last-seen), with the
    current one flagged."""
    current_id = getattr(request.state, "session_id", None)
    rows = await _sessions_for(session, principal.id)
    return [_session_out(r, current_id) for r in rows]


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_my_session(
    session_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke one of the caller's OWN sessions (remote logout). 404 if the id is
    unknown or belongs to another principal (never leak another user's session).
    The revoked session dies on its very next request (instant revocation)."""
    row = await session.get(SessionRow, session_id)
    if row is None or row.principal_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await session.delete(row)
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=principal.id,
        details={"session_id": str(session_id), "self": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/sessions/revoke-all", status_code=204)
async def revoke_all_my_sessions(
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """"Log out everywhere" — kill every one of the caller's sessions (including
    this one). Other principals' sessions are untouched."""
    n = await authx.revoke_all_for_principal(session, str(principal.id))
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=principal.id,
        details={"scope": "all", "count": n, "self": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/auth/users/{principal_id}/sessions",
    response_model=list[SessionOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_user_sessions(
    principal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    """Admin: list any principal's active sessions."""
    rows = await _sessions_for(session, principal_id)
    return [_session_out(r, None) for r in rows]


@router.delete(
    "/auth/users/{principal_id}/sessions",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def revoke_user_sessions(
    principal_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Admin: force-log-out a principal everywhere (kills only that principal's
    sessions)."""
    n = await authx.revoke_all_for_principal(session, str(principal_id))
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal_id), "scope": "all", "count": n},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
