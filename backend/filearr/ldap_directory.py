"""AD/LDAP directory enumeration (LDAP-T1, 2026-08-20).

The CENTRAL-only half of "attribute permissions to accounts and AD objects".
Windows ACL entries name a principal by **SID**; agents push the bare SID when
they cannot resolve it (non-domain-joined, or no DC reachable). This module
enumerates the directory — users and groups — capturing each object's
``objectSid`` / ``objectGUID`` / ``sAMAccountName`` / ``displayName`` /
``memberOf``, so :mod:`filearr.worker`'s ``sync_directory`` task can store them
in ``directory_objects`` and reconcile the SIDs a permission snapshot carries
into named identities (``principal_aliases``) + group-membership expansion.

It reuses the login stack's transport/bind config (:class:`ldap_auth.LdapConfig`,
:func:`ldap_auth.connect`) and the same injected-connector seam, so the offline
``MOCK_SYNC`` harness exercises the full flow with no sockets. Pure decoding
(SID/GUID from the raw binary AD returns) is unit-tested directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from filearr.config import Settings, get_settings
from filearr.ldap_auth import LdapConfig, LDAPError, _bind_service, _safe_unbind, connect

logger = logging.getLogger("filearr.ldap_directory")


# --------------------------------------------------------------------------- #
# Binary decoders (AD returns objectSid / objectGUID as raw bytes)            #
# --------------------------------------------------------------------------- #
def decode_sid(raw: bytes) -> str | None:
    """Decode a binary ``objectSid`` to its canonical ``S-1-5-…`` string.

    Layout (MS-DTYP §2.4.2.2): byte 0 = revision, byte 1 = sub-authority count,
    bytes 2..7 = 48-bit identifier authority (BIG-endian), then count × 32-bit
    sub-authorities (LITTLE-endian). Returns ``None`` on a malformed buffer
    (never raises — a bad attribute must not kill a whole sync)."""
    if not raw or len(raw) < 8:
        return None
    try:
        revision = raw[0]
        sub_count = raw[1]
        authority = int.from_bytes(raw[2:8], "big")
        if len(raw) < 8 + 4 * sub_count:
            return None
        subs = [
            int.from_bytes(raw[8 + 4 * i : 12 + 4 * i], "little") for i in range(sub_count)
        ]
        return "S-" + "-".join([str(revision), str(authority), *map(str, subs)])
    except (ValueError, IndexError):
        return None


def decode_guid(raw: bytes) -> str | None:
    """Decode a binary ``objectGUID`` (16 bytes) to its canonical string form.

    AD stores the GUID mixed-endian: the first three fields are little-endian,
    the last two big-endian — so the string is
    ``d3d2d1d0-d5d4-d7d6-d8d9-d10d11d12d13d14d15``. Returns ``None`` if the
    buffer is not 16 bytes."""
    if not raw or len(raw) != 16:
        return None
    b = raw
    try:
        return (
            f"{b[3]:02x}{b[2]:02x}{b[1]:02x}{b[0]:02x}-"
            f"{b[5]:02x}{b[4]:02x}-{b[7]:02x}{b[6]:02x}-"
            f"{b[8]:02x}{b[9]:02x}-"
            f"{b[10]:02x}{b[11]:02x}{b[12]:02x}{b[13]:02x}{b[14]:02x}{b[15]:02x}"
        )
    except IndexError:
        return None


def domain_from_dn(dn: str | None) -> str | None:
    """Derive a NetBIOS-ish domain label from a DN's ``dc=`` components: the
    FIRST ``dc=`` value, upper-cased (``…,dc=corp,dc=example,dc=com`` → ``CORP``).
    A best-effort default when the object carries no explicit domain; the
    operator can override with ``FILEARR_LDAP_DIRECTORY_DOMAIN``."""
    if not dn:
        return None
    for part in dn.split(","):
        part = part.strip()
        if part[:3].lower() == "dc=":
            return part[3:].upper() or None
    return None


def _kind_from_classes(classes: list[str]) -> str:
    low = {str(c).lower() for c in (classes or [])}
    if "group" in low or "groupofnames" in low or "posixgroup" in low:
        return "group"
    if "computer" in low:
        return "computer"
    if low & {"user", "person", "inetorgperson", "organizationalperson", "posixaccount"}:
        return "user"
    return "other"


# userAccountControl ACCOUNTDISABLE bit (MS-ADTS).
_UAC_ACCOUNTDISABLE = 0x2


@dataclass(slots=True)
class DirEntry:
    """One decoded directory object, ready to upsert into ``directory_objects``."""

    object_guid: str
    object_sid: str | None
    sam_account_name: str | None
    display_name: str | None
    user_principal_name: str | None
    distinguished_name: str | None
    kind: str
    domain: str | None
    member_of_dns: tuple[str, ...] = ()
    disabled: bool = False

    def canonical_id(self) -> str:
        """The cross-host canonical identity this object resolves a raw SID to:
        ``DOMAIN\\sam`` when both are known, else the ``userPrincipalName``, else
        the SID, else the GUID. Never empty."""
        if self.domain and self.sam_account_name:
            return f"{self.domain}\\{self.sam_account_name}"
        return (
            self.user_principal_name
            or self.object_sid
            or self.sam_account_name
            or self.object_guid
        )

    def display(self) -> str | None:
        return self.display_name or self.sam_account_name or self.user_principal_name


@dataclass(slots=True)
class DirectoryConfig:
    """Directory-sync-specific knobs, read from settings alongside the shared
    :class:`LdapConfig` transport/bind config."""

    user_base: str | None
    group_base: str | None
    user_filter: str
    group_filter: str
    attr_sid: str
    attr_guid: str
    attr_display: str
    attr_sam: str
    attr_upn: str
    attr_member_of: str
    domain_override: str | None
    page_size: int
    max_objects: int

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> DirectoryConfig:
        s = s or get_settings()
        return cls(
            user_base=(s.ldap_directory_user_base or s.ldap_user_base or None),
            group_base=(s.ldap_directory_group_base or s.ldap_group_base or None),
            user_filter=s.ldap_directory_user_filter,
            group_filter=s.ldap_directory_group_filter,
            attr_sid=s.ldap_attr_object_sid,
            attr_guid=s.ldap_attr_object_guid,
            attr_display=s.ldap_attr_display_name,
            attr_sam=s.ldap_attr_sam,
            attr_upn=s.ldap_attr_upn,
            attr_member_of=s.ldap_attr_member_of_dir,
            domain_override=(s.ldap_directory_domain or None),
            page_size=max(1, min(int(s.ldap_directory_page_size), 1000)),
            max_objects=int(s.ldap_directory_max_objects),
        )


@dataclass(slots=True)
class DirectoryEndpoint:
    """One directory to enumerate: its transport/bind (:class:`LdapConfig`), its
    directory knobs (:class:`DirectoryConfig`), and a ``label`` (the host) used
    as ``directory_objects.source_directory`` so tombstoning stays per-endpoint.

    Cross-forest = several endpoints, each its own bind. Multi-domain within a
    forest = one endpoint pointed at a Global Catalog (whose subtree spans child
    domains); each object's own DN still yields its domain."""

    label: str
    ldap: LdapConfig
    dcfg: DirectoryConfig


def endpoints_from_settings(s: Settings | None = None) -> list[DirectoryEndpoint]:
    """Resolve the configured directory endpoints.

    ``ldap_directories`` (JSON list) → one endpoint per entry, each overriding
    the global ``ldap_*`` config for its own server/bind/bases/domain (omitted
    keys fall back to the globals). EMPTY → a single endpoint from the global
    ``ldap_directory_*`` config (back-compat). A malformed entry (no ``server``,
    or an ``LdapConfig`` the transport policy rejects) raises :class:`LDAPError`
    naming the offending endpoint, rather than silently dropping a forest."""
    s = s or get_settings()
    entries = s.ldap_directories or []
    if not entries:
        return [
            DirectoryEndpoint(
                label=LdapConfig.from_settings(s).host,
                ldap=LdapConfig.from_settings(s),
                dcfg=DirectoryConfig.from_settings(s),
            )
        ]
    out: list[DirectoryEndpoint] = []
    for i, entry in enumerate(entries):
        server = (entry.get("server") or "").strip()
        if not server:
            raise LDAPError("bad_directory", f"ldap_directories[{i}] has no 'server'")
        # Build a per-endpoint Settings overlay so LdapConfig.from_settings keeps
        # owning the (security-critical) transport policy — no duplicate of it.
        overlay = {
            "ldap_enabled": True,
            "ldap_server": server,
            "ldap_bind_dn": entry.get("bind_dn", s.ldap_bind_dn),
            "ldap_bind_password": entry.get("bind_password", s.ldap_bind_password),
            "ldap_start_tls": entry.get("start_tls", s.ldap_start_tls),
            "ldap_allow_plaintext": entry.get("allow_plaintext", s.ldap_allow_plaintext),
            "ldap_tls_verify": entry.get("tls_verify", s.ldap_tls_verify),
            "ldap_tls_ca_cert_file": entry.get("tls_ca_cert_file", s.ldap_tls_ca_cert_file),
            "ldap_tls_ca_cert_pem": entry.get("tls_ca_cert_pem", s.ldap_tls_ca_cert_pem),
            # user_base satisfies LdapConfig.from_settings' "needs a base" guard.
            "ldap_user_base": entry.get("user_base") or s.ldap_user_base or server,
            "ldap_directory_user_base": entry.get("user_base", s.ldap_directory_user_base),
            "ldap_directory_group_base": entry.get("group_base", s.ldap_directory_group_base),
            "ldap_directory_user_filter": entry.get("user_filter", s.ldap_directory_user_filter),
            "ldap_directory_group_filter": entry.get("group_filter", s.ldap_directory_group_filter),
            "ldap_directory_domain": entry.get("domain", s.ldap_directory_domain),
            "ldap_directory_page_size": int(entry.get("page_size", s.ldap_directory_page_size)),
        }
        try:
            ep_settings = s.model_copy(update=overlay)
            ldap_cfg = LdapConfig.from_settings(ep_settings)
        except LDAPError as exc:
            raise LDAPError(exc.reason, f"ldap_directories[{i}] ({server}): {exc.detail}") from exc
        out.append(
            DirectoryEndpoint(
                label=(entry.get("label") or ldap_cfg.host),
                ldap=ldap_cfg,
                dcfg=DirectoryConfig.from_settings(ep_settings),
            )
        )
    return out


def _raw_first(entry, name: str) -> bytes | None:
    """First RAW (bytes) value of an attribute, or None. Binary AD attributes
    (objectSid/objectGUID) must be read from raw_attributes to stay bytes."""
    try:
        vals = entry["raw_attributes"].get(name) if "raw_attributes" in entry else None
    except (TypeError, KeyError):
        vals = None
    if not vals:
        return None
    v = vals[0]
    return v if isinstance(v, bytes) else bytes(v)


def _text_first(entry, name: str) -> str | None:
    try:
        vals = entry["attributes"].get(name) if "attributes" in entry else None
    except (TypeError, KeyError):
        vals = None
    if not vals:
        return None
    v = vals[0] if isinstance(vals, list) else vals
    return str(v) if v is not None else None


def _text_all(entry, name: str) -> list[str]:
    try:
        vals = entry["attributes"].get(name) if "attributes" in entry else None
    except (TypeError, KeyError):
        vals = None
    if not vals:
        return []
    return [str(v) for v in (vals if isinstance(vals, list) else [vals])]


def _decode_entry(entry, dcfg: DirectoryConfig) -> DirEntry | None:
    """Turn one ldap3 search entry dict into a :class:`DirEntry`, or None when it
    has no usable stable key (objectGUID) — such an object cannot be tracked."""
    guid = decode_guid(_raw_first(entry, dcfg.attr_guid) or b"")
    if guid is None:
        # OpenLDAP may hand entryUUID as text, not binary — accept that too.
        guid = _text_first(entry, dcfg.attr_guid)
    if not guid:
        return None
    sid = decode_sid(_raw_first(entry, dcfg.attr_sid) or b"")
    if sid is None:
        sid = _text_first(entry, dcfg.attr_sid)  # already-string SID (rare)
    dn = entry.get("dn") if isinstance(entry, dict) else None
    classes = _text_all(entry, "objectClass")
    uac_raw = _text_first(entry, "userAccountControl")
    disabled = False
    if uac_raw is not None:
        try:
            disabled = bool(int(uac_raw) & _UAC_ACCOUNTDISABLE)
        except ValueError:
            disabled = False
    return DirEntry(
        object_guid=guid,
        object_sid=sid,
        sam_account_name=_text_first(entry, dcfg.attr_sam),
        display_name=_text_first(entry, dcfg.attr_display),
        user_principal_name=_text_first(entry, dcfg.attr_upn),
        distinguished_name=dn,
        kind=_kind_from_classes(classes),
        domain=dcfg.domain_override or domain_from_dn(dn),
        member_of_dns=tuple(_text_all(entry, dcfg.attr_member_of)),
        disabled=disabled,
    )


def _paged(conn, base: str, ldap_filter: str, attrs: list[str], dcfg: DirectoryConfig):
    """Yield search-entry dicts, page by page. ldap3's paged_search generator
    works under both a live server and the offline MOCK_SYNC harness."""
    yield from conn.extend.standard.paged_search(
        search_base=base,
        search_filter=ldap_filter,
        attributes=attrs,
        paged_size=dcfg.page_size,
        generator=True,
    )


def enumerate_directory(
    cfg: LdapConfig,
    dcfg: DirectoryConfig | None = None,
    *,
    connector=connect,
) -> list[DirEntry]:
    """Enumerate AD users + groups and return decoded :class:`DirEntry` rows.

    Requires a SERVICE bind (``ldap_bind_dn``/``ldap_bind_password``) — anonymous
    enumeration of a whole directory is neither typical nor safe. Raises
    :class:`LDAPError` on a transport/bind failure so the caller records a failed
    sync; a per-entry decode failure is skipped (logged), never fatal. Bounded by
    ``max_objects``."""
    dcfg = dcfg or DirectoryConfig.from_settings()
    if not cfg.bind_dn:
        raise LDAPError(
            "no_service_bind",
            "directory enumeration needs a service bind (ldap_bind_dn/password)",
        )
    if not (dcfg.user_base or dcfg.group_base):
        raise LDAPError("no_base", "directory sync needs a user or group search base")

    text_attrs = [
        "objectClass",
        dcfg.attr_sam,
        dcfg.attr_display,
        dcfg.attr_upn,
        dcfg.attr_member_of,
        "userAccountControl",
    ]
    # objectSid/objectGUID come back through raw_attributes; ldap3 still needs
    # them named in the requested attribute list.
    attrs = [*text_attrs, dcfg.attr_sid, dcfg.attr_guid]

    conn = _bind_service(cfg, connector)
    out: list[DirEntry] = []
    try:
        passes = [
            (dcfg.user_base, dcfg.user_filter),
            (dcfg.group_base, dcfg.group_filter),
        ]
        seen: set[str] = set()
        for base, ldap_filter in passes:
            if not base:
                continue
            for raw in _paged(conn, base, ldap_filter, attrs, dcfg):
                if raw.get("type") not in (None, "searchResEntry"):
                    continue  # skip referrals / result markers
                de = _decode_entry(raw, dcfg)
                if de is None or de.object_guid in seen:
                    continue
                seen.add(de.object_guid)
                out.append(de)
                if len(out) >= dcfg.max_objects:
                    logger.warning(
                        "directory sync hit max_objects=%d — truncating; raise "
                        "FILEARR_LDAP_DIRECTORY_MAX_OBJECTS or narrow the base",
                        dcfg.max_objects,
                    )
                    return out
    finally:
        _safe_unbind(conn)
    return out


async def expand_principals_with_groups(
    session, principals: set[str], *, max_depth: int = 16
) -> set[str]:
    """Grow a caller's identity closure by their AD group memberships.

    Given the SIDs/ids a caller answers to, add every group SID they belong to,
    transitively (nested groups), using ``directory_objects.member_of_sids``.
    A grant to a group then correctly attributes to its members in
    :func:`permissions.effective_access`. Bounded depth guards against a cyclic
    membership graph (AD forbids cycles, but a stale sync could hold one). Only
    the group SIDs are added — the original identities are always preserved. A
    no-op when the directory has never synced (the table is empty)."""
    from sqlalchemy import select

    from filearr.models import DirectoryObject

    closure = set(principals)
    frontier = set(principals)
    for _ in range(max_depth):
        if not frontier:
            break
        rows = (
            await session.execute(
                select(DirectoryObject.member_of_sids).where(
                    DirectoryObject.object_sid.in_(frontier)
                )
            )
        ).all()
        new: set[str] = set()
        for (member_of,) in rows:
            for gsid in member_of or ():
                if gsid not in closure:
                    new.add(gsid)
        closure |= new
        frontier = new
    return closure


@dataclass(slots=True)
class ReconcileResult:
    objects: int = 0
    users: int = 0
    groups: int = 0
    tombstoned: int = 0
    aliases_written: int = 0
    unresolved_sids: int = 0
    memberships_expanded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "objects": self.objects,
            "users": self.users,
            "groups": self.groups,
            "tombstoned": self.tombstoned,
            "aliases_written": self.aliases_written,
            "unresolved_sids": self.unresolved_sids,
            "memberships_expanded": self.memberships_expanded,
            "errors": self.errors,
        }
