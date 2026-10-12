"""Session-based authentication and role guards (enforced server-side).

Roles (hierarchical): guest < expert < admin.
In operations, the local login would be replaced by OIDC/SAML (e.g. Keycloak federated with the
agency identity provider); the role guards stay unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select

from ..models import User
from ..security import has_role


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: str
    full_name: str

    @property
    def is_expert(self) -> bool:
        return has_role(self.role, "expert")

    @property
    def is_admin(self) -> bool:
        return has_role(self.role, "admin")


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, required: str):
        self.required = required


def current_user(request: Request) -> CurrentUser:
    data = request.session.get("user")
    if not data:
        raise LoginRequired()
    # Re-validate against the DB so role changes / deactivation take effect immediately.
    with request.app.state.services.db.session() as s:
        u = s.scalar(select(User).where(User.username == data["username"]))
        if u is None or not u.active:
            request.session.clear()
            raise LoginRequired()
        if u.role != data["role"]:
            data = {**data, "role": u.role}
            request.session["user"] = data
    return CurrentUser(**data)


def require(role: str):
    def dep(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not has_role(user.role, role):
            raise Forbidden(role)
        return user
    return dep


require_guest = require("guest")
require_expert = require("expert")
require_admin = require("admin")
