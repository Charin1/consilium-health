"""
Lightweight auth context and role-based access control dependencies.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel


VALID_ROLES = {"owner", "admin", "consultant", "viewer"}


class CurrentUser(BaseModel):
    user_id: str
    role: str
    workspace_id: str


def _normalize_role(role: str) -> str:
    cleaned = role.strip().lower()
    return cleaned if cleaned in VALID_ROLES else "viewer"


async def get_current_user(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
) -> CurrentUser:
    """
    Resolve current user from headers.
    Defaults are dev-friendly until a full auth provider is integrated.
    """
    return CurrentUser(
        user_id=(x_user_id or "demo-user").strip(),
        role=_normalize_role(x_user_role or "consultant"),
        workspace_id=(x_workspace_id or "default-workspace").strip(),
    )


def require_roles(*allowed_roles: str) -> Callable:
    """Require one of the provided roles for endpoint access."""
    normalized_allowed = {_normalize_role(role) for role in allowed_roles}

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in normalized_allowed:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action")
        return user

    return dependency


def can_view_workspace_data(role: str) -> bool:
    return _normalize_role(role) in {"owner", "admin", "consultant", "viewer"}


def can_edit_workspace_data(role: str) -> bool:
    return _normalize_role(role) in {"owner", "admin", "consultant"}


def validate_roles(roles: Iterable[str]) -> None:
    invalid = [role for role in roles if _normalize_role(role) not in VALID_ROLES]
    if invalid:
        raise ValueError(f"Invalid roles configured: {invalid}")
