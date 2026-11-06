"""User administration: promote, demote, disable, enable.

The columns this manages -- users.is_admin and users.disabled_at -- existed from
the first migration, with nothing to change them but hand-written SQL. Every
route here is ADMIN-ONLY, on top of the ordinary scope guard:

    read  scope  + admin   list users, look at one
    run   scope  + admin   change one

Both conditions, never either. Admin widens WHOSE data a credential reaches;
scope bounds WHICH ACTIONS it may take. An admin's read-only key can look at
the user list and change nothing, exactly as it can look at every run and
cancel none (see api/auth.py:Principal).

Who is an admin is decided per request from the database (api/users.py rebuilds
the Principal on every call), so a promotion or demotion takes effect on the
target's very next request -- nothing here has to flush a session to make it so.

DISABLING STOPS EVERYTHING ACTING AS THE USER, not just their credentials:
console sessions are revoked, queued and running runs are cancelled through the
same registry.cancel the /runs/{id}/cancel route uses (the worker stops a
running container when it sees the state), and any live login browser on their
account is stopped. Their run history and connected social accounts are KEPT --
disabling is reversible, and the schema says so. API keys are SUSPENDED, not
revoked: refused while disabled, working again once enabled.

LOCKOUT. An admin cannot demote or disable themselves, and no change may leave
the platform with zero enabled admins (enforced in api/users.py, under a lock,
so two admins demoting each other at once cannot both succeed). The operator
key from the environment is not a user and does not count -- it is, though, how
the FIRST admin is promoted on a fresh install.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from api import invites, throttle
from api.auth import Principal
from api.validation import SafeText
from api.users import (
    LastAdmin,
    NoSuchUser,
    User,
    list_users,
    set_admin,
    set_disabled,
    user_detail,
)
from db.pool import pool
from shared.logging import event
from shared.models import RunStatus

# Recorded as the cancelled run's error, so its owner (once re-enabled) and an
# operator reading the run both see why it stopped.
CANCEL_REASON = "owner disabled by an administrator"


class AdminUser(BaseModel):
    id: str
    email: str
    username: str
    is_admin: bool
    disabled: bool
    # What the person calls themselves (their profile). Shown beside the
    # username, never instead of it: the username is who they are.
    display_name: str | None = None
    disabled_at: object = None
    created_at: object = None
    # Locked out by too many failed sign-ins (api/throttle.py). Temporary and
    # separate from `disabled`: it clears by itself, or with POST .../unlock.
    locked: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AdminUser:
        return cls(id=str(row["id"]), email=row["email"], username=row["username"],
                   display_name=row.get("display_name"),
                   is_admin=bool(row["is_admin"]), disabled=row["disabled_at"] is not None,
                   disabled_at=row["disabled_at"], created_at=row["created_at"],
                   locked=throttle.locked_identifiers(row["username"], row["email"]))


class AdminUserDetail(AdminUser):
    live_sessions: int
    api_keys: int
    runs: int


class AdminUserList(BaseModel):
    users: list[AdminUser]
    count: int


class AdminChange(BaseModel):
    """The user after the change, and whether it changed anything."""

    user: AdminUser
    # True when the user was ALREADY in the requested state -- the change is
    # idempotent, as cancelling a cancelled run is.
    already: bool


class DeleteResult(BaseModel):
    deleted: bool
    runs_deleted: int = 0
    browser_profiles_removed: int = 0


class DisableResult(AdminChange):
    sessions_revoked: bool = False
    runs_cancelled: int = 0
    # Runs that finished between being listed and being cancelled. Not an
    # error -- they simply were not live any more.
    runs_already_finished: int = 0
    login_browsers_stopped: int = 0
    # Closed in the database, but the container could not be confirmed stopped
    # (no Docker on this host, say). The reaper collects it; reported so it is
    # not silently assumed gone.
    login_browsers_unconfirmed: int = 0



class Invite(BaseModel):
    """An invite as an admin sees it: never the token, which is not stored."""

    id: str
    note: str | None = None
    status: str          # pending / used / revoked / expired
    created_at: object = None
    expires_at: object = None
    used_at: object = None
    created_by: str | None = None   # username; None for the operator key
    used_by: str | None = None


class InviteList(BaseModel):
    invites: list[Invite]
    count: int
    signup: str          # the current ACTOR_PLATFORM_SIGNUP mode


class CreateInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: SafeText | None = Field(default=None, max_length=200,
                                  description="Who it is for. Shown only to admins.")
    days: int | None = Field(default=None, ge=1, le=invites.MAX_INVITE_DAYS,
                             description="Days until it expires. Default "
                                         "ACTOR_PLATFORM_INVITE_DAYS (7).")


class NewInvite(Invite):
    # Returned exactly once. Only its sha256 is stored, so it cannot be shown
    # again: an admin who loses it revokes the invite and makes another.
    token: str

def _user_row(user: User) -> AdminUser:
    detail = user_detail(user.id)
    return AdminUser.from_row(detail) if detail else AdminUser(
        id=user.id, email=user.email, username=user.username,
        display_name=user.display_name, is_admin=user.is_admin, disabled=user.disabled)


def build_router(registry, current_reader, current_operator) -> APIRouter:
    """`current_reader` / `current_operator` are main.py's read- and
    run-scoped guards; admin is checked here, on top of them."""
    from api.main import PlatformError  # noqa: PLC0415 - avoids a cycle

    router = APIRouter(tags=["admin"])

    def _require_admin(principal: Principal) -> None:
        if not principal.is_admin:
            # 403, not 404: the route is static and documented, so refusing it
            # reveals nothing about any user.
            raise PlatformError(403, "admin only",
                                "this route is for platform administrators")

    def _target(user_id: str) -> str:
        """A well-formed user id, or 404. Never the 500 a uuid cast in SQL
        would produce for 'not-a-uuid'."""
        try:
            return str(uuid.UUID(user_id))
        except ValueError:
            raise PlatformError(404, "no such user", f"no user {user_id}") from None

    def _not_self(principal: Principal, target: str, action: str) -> None:
        if principal.user_id and str(principal.user_id) == target:
            raise PlatformError(
                409, "cannot change yourself",
                f"an administrator cannot {action} their own account -- "
                "ask another administrator",
            )

    def _change(fn, target: str, value: bool):
        try:
            return fn(target, value)
        except NoSuchUser as exc:
            raise PlatformError(404, "no such user", f"no user {target}") from exc
        except LastAdmin as exc:
            raise PlatformError(409, "last administrator", str(exc)) from exc

    def _by(principal: Principal) -> str:
        return str(principal.user_id) if principal.user_id else "operator"

    # --- reads ------------------------------------------------------------

    @router.get("/admin/users", response_model=AdminUserList)
    def admin_list_users(
        principal: Principal = current_reader,
        q: str | None = Query(default=None, max_length=200,
                              description="substring of the email or username"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AdminUserList:
        _require_admin(principal)
        rows = [AdminUser.from_row(r) for r in list_users(q, limit)]
        return AdminUserList(users=rows, count=len(rows))

    @router.get("/admin/users/{user_id}", response_model=AdminUserDetail)
    def admin_get_user(user_id: str, principal: Principal = current_reader) -> AdminUserDetail:
        _require_admin(principal)
        row = user_detail(_target(user_id))
        if row is None:
            raise PlatformError(404, "no such user", f"no user {user_id}")
        return AdminUserDetail(**AdminUser.from_row(row).model_dump(),
                               live_sessions=row["live_sessions"],
                               api_keys=row["api_keys"], runs=row["runs"])

    # --- writes -----------------------------------------------------------

    @router.post("/admin/users/{user_id}/promote", response_model=AdminChange)
    def promote(user_id: str, principal: Principal = current_operator) -> AdminChange:
        _require_admin(principal)
        target = _target(user_id)
        user, changed = _change(set_admin, target, True)
        if changed:
            event("api", "user_promoted", target_user=target, by_user=_by(principal))
        return AdminChange(user=_user_row(user), already=not changed)

    @router.post("/admin/users/{user_id}/demote", response_model=AdminChange)
    def demote(user_id: str, principal: Principal = current_operator) -> AdminChange:
        _require_admin(principal)
        target = _target(user_id)
        _not_self(principal, target, "demote")
        user, changed = _change(set_admin, target, False)
        if changed:
            event("api", "user_demoted", target_user=target, by_user=_by(principal))
        return AdminChange(user=_user_row(user), already=not changed)

    @router.delete("/admin/users/{user_id}", response_model=DeleteResult)
    def delete_user(user_id: str, principal: Principal = current_operator) -> DeleteResult:
        """Delete a user and everything that is theirs (security review M-04):
        runs and their files, API keys, sessions, connected accounts and the
        browser profiles kept for them. Irreversible, unlike disable. An admin
        must be demoted first, so the last-admin rule keeps holding."""
        from registry.run_registry import RegistryError  # noqa: PLC0415
        from shared.state_dirs import remove_account_state  # noqa: PLC0415

        _require_admin(principal)
        target = _target(user_id)
        _not_self(principal, target, "delete")
        row = user_detail(target)
        if row is None:
            raise PlatformError(404, "no such user", f"no user {user_id}")
        if row["is_admin"]:
            raise PlatformError(409, "demote first",
                                "an administrator must be demoted before being deleted")

        # Stop everything acting as them first, exactly as disable does.
        _change(set_disabled, target, True)
        _cancel_live_runs(target)
        _stop_login_browsers(target)

        deleted = kept = 0
        if hasattr(registry, "for_owner"):
            for run in registry.for_owner(target).list():
                try:
                    registry.delete(run.run_id)
                    deleted += 1
                except RegistryError:
                    kept += 1  # still live a moment after cancel; removed below
        with pool().connection() as conn:
            accounts = [str(r["id"]) for r in conn.execute(
                "SELECT id FROM social_accounts WHERE user_id = %s", (target,)).fetchall()]
            # Anything left (a run still winding down) goes with the row.
            conn.execute("DELETE FROM runs WHERE owner_id = %s", (target,))
            conn.execute("DELETE FROM users WHERE id = %s", (target,))
        profiles = sum(remove_account_state(a) for a in accounts)
        event("api", "user_deleted", target_user=target, by_user=_by(principal),
              runs_deleted=deleted + kept, profiles_removed=profiles)
        return DeleteResult(deleted=True, runs_deleted=deleted + kept,
                            browser_profiles_removed=profiles)

    # --- invites (api/invites.py) ------------------------------------------

    @router.get("/admin/invites", response_model=InviteList)
    def admin_list_invites(principal: Principal = current_reader,
                           limit: int = Query(default=100, ge=1, le=500)) -> InviteList:
        _require_admin(principal)
        rows = [Invite(**r) for r in invites.list_invites(limit)]
        return InviteList(invites=rows, count=len(rows), signup=invites.signup_mode())

    @router.post("/admin/invites", response_model=NewInvite, status_code=201)
    def admin_create_invite(body: CreateInviteRequest | None = None,
                            principal: Principal = current_operator) -> NewInvite:
        _require_admin(principal)
        body = body or CreateInviteRequest()
        row, token = invites.create_invite(
            str(principal.user_id) if principal.user_id else None, body.note, body.days)
        # The id, never the token: the event log is readable by operators,
        # and a token in it would be a working invite.
        event("api", "invite_created", invite_id=row["id"], by_user=_by(principal),
              expires_at=str(row["expires_at"]))
        return NewInvite(**row, token=token)

    @router.delete("/admin/invites/{invite_id}", response_model=Invite)
    def admin_revoke_invite(invite_id: str, principal: Principal = current_operator) -> Invite:
        _require_admin(principal)
        try:
            target = str(uuid.UUID(invite_id))
        except ValueError:
            raise PlatformError(404, "no such invite", f"no invite {invite_id}") from None
        row = invites.revoke_invite(target)
        if row is None:
            raise PlatformError(404, "no such invite", f"no invite {invite_id}")
        event("api", "invite_revoked", invite_id=target, by_user=_by(principal),
              status=row["status"])
        return Invite(**row)

    @router.post("/admin/users/{user_id}/unlock", response_model=AdminChange)
    def unlock(user_id: str, principal: Principal = current_operator) -> AdminChange:
        """Clear a sign-in lockout (security review H-02) before it expires."""
        _require_admin(principal)
        target = _target(user_id)
        row = user_detail(target)
        if row is None:
            raise PlatformError(404, "no such user", f"no user {user_id}")
        cleared = throttle.unlock_user(row["username"], row["email"])
        if cleared:
            event("api", "user_unlocked", target_user=target, by_user=_by(principal))
        return AdminChange(user=AdminUser.from_row(row), already=not cleared)

    @router.post("/admin/users/{user_id}/enable", response_model=AdminChange)
    def enable(user_id: str, principal: Principal = current_operator) -> AdminChange:
        _require_admin(principal)
        target = _target(user_id)
        user, changed = _change(set_disabled, target, False)
        if changed:
            event("api", "user_enabled", target_user=target, by_user=_by(principal))
        return AdminChange(user=_user_row(user), already=not changed)

    @router.post("/admin/users/{user_id}/disable", response_model=DisableResult)
    def disable(user_id: str, principal: Principal = current_operator) -> DisableResult:
        _require_admin(principal)
        target = _target(user_id)
        _not_self(principal, target, "disable")
        # The flag FIRST: from its commit on, no credential of theirs works, so
        # nothing can start new work while the rest is being stopped.
        user, changed = _change(set_disabled, target, True)

        # The stopping runs on EVERY call, not only the one that changed the
        # flag: a retry after a partial failure must finish the job, and on an
        # already-stopped user each step is a no-op.
        cancelled, finished = _cancel_live_runs(target)
        stopped, unconfirmed = _stop_login_browsers(target)

        event("api", "user_disabled", target_user=target, by_user=_by(principal),
              already=not changed, runs_cancelled=cancelled,
              login_browsers_stopped=stopped + unconfirmed)
        return DisableResult(
            user=_user_row(user), already=not changed, sessions_revoked=True,
            runs_cancelled=cancelled, runs_already_finished=finished,
            login_browsers_stopped=stopped, login_browsers_unconfirmed=unconfirmed,
        )

    def _cancel_live_runs(target: str) -> tuple[int, int]:
        """Cancel the user's PENDING and RUNNING runs. -> (cancelled, raced)."""
        from registry.run_registry import CancelTooLate, RegistryError  # noqa: PLC0415

        if not hasattr(registry, "for_owner"):
            return 0, 0  # a registry with no owners has no runs that are theirs
        theirs = registry.for_owner(target)
        live = [run for status in (RunStatus.PENDING, RunStatus.RUNNING)
                for run in theirs.list(status=status)]
        cancelled = finished = 0
        for run in live:
            try:
                registry.cancel(run.run_id, reason=CANCEL_REASON)
                cancelled += 1
            except CancelTooLate:
                finished += 1  # it completed while we were getting to it
            except RegistryError as exc:
                event("api", "user_disable_cancel_failed", target_user=target,
                      run_id=run.run_id, error=str(exc))
        return cancelled, finished

    def _stop_login_browsers(target: str) -> tuple[int, int]:
        """Stop the user's live login browsers. -> (stopped, unconfirmed).

        Demo build: there is no login browser (a connect is an in-memory record
        in api/social.py), so there is never anything to stop. The counts stay
        in the response so its shape matches the real build's.
        """
        return 0, 0

    return router
