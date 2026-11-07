"""Render pydantic models as form fields and parse submitted forms back (one generic path for all pages)."""

import typing
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, SecretStr, ValidationError

SECRET_SET = "••• set"
LABELS = {
    "url": "Address", "verify_tls": "Verify TLS certificate", "backup_enabled": "Back up this Miniserver",
    "auth_mode": "Auth mode", "backup_user": "User", "backup_password": "Password",
    "bridge_enabled": "Stream live data", "bridge_user": "User", "bridge_password": "Password",
    "keep": "Snapshots to keep", "path": "Folder", "access_key": "Access key", "secret_key": "Secret key",
    "ha_discovery": "Home Assistant discovery", "id": "ID", "sse": "Server-side encryption",
    "ntfy_url": "ntfy topic URL", "gotify_url": "Gotify URL", "smtp_starttls": "STARTTLS", "smtp_to": "Send to",
    "smtp_from": "From", "on": "Notify on", "healthcheck_success_url": "Ping on success",
    "healthcheck_fail_url": "Ping on failure", "webhook_url": "Webhook URL",
}


def _kind(ann: Any) -> tuple[str, list[str]]:
    if get_origin(ann) is Literal:
        return "select", [str(a) for a in get_args(ann)]
    if ann is bool:
        return "bool", []
    if ann is SecretStr:
        return "secret", []
    if ann in (int, float):
        return "number", []
    if get_origin(ann) is list:
        return "list", []
    return "text", []


def fields(model: BaseModel, path: str, locked: set[str], skip: set[str] = frozenset()) -> list[dict]:  # type: ignore[assignment]
    out = []
    for name, f in type(model).model_fields.items():
        if name in skip:
            continue
        kind, options = _kind(f.annotation)
        value = getattr(model, name)
        if kind == "secret":
            value = SECRET_SET if value.get_secret_value() else ""
        elif kind == "list":
            value = "\n".join(map(str, value))
        out.append({
            "name": name,
            "label": LABELS.get(name) or name.replace("_", " ").capitalize(),
            "kind": kind,
            "options": options,
            "value": value,
            "step": "any" if f.annotation is float else "1",
            "locked": f"{path}.{name}" in locked,
            "help": f.description or "",
        })
    return out


def parse(model_cls: type[BaseModel], form: typing.Mapping[str, Any], existing: BaseModel | None,
          skip: set[str] = frozenset()) -> BaseModel:  # type: ignore[assignment]
    """Raises ValidationError. Empty secret = keep the stored one. Skipped fields keep their old value."""
    data: dict[str, Any] = existing.model_dump() if existing else {}
    for name, f in model_cls.model_fields.items():
        if name in skip:
            continue
        kind, _ = _kind(f.annotation)
        raw = form.get(name)
        if kind == "bool":
            data[name] = raw is not None
        elif kind == "secret":
            if raw and raw != SECRET_SET:
                data[name] = raw
            elif existing is not None:
                data[name] = getattr(existing, name)
        elif kind == "list":
            if raw is not None:
                data[name] = [x.strip() for x in str(raw).splitlines() if x.strip()]
        elif raw is not None:
            data[name] = raw
    return model_cls.model_validate(data)


def errors(e: ValidationError) -> list[str]:
    return [f"{'.'.join(map(str, err['loc'])) or 'form'}: {err['msg']}" for err in e.errors()]


def sections(flds: list[dict], spec: list[tuple[str, list[str]]]) -> list[tuple[str, list[dict]]]:
    """Group fields into titled sections in the given order; everything not listed goes to "Advanced"."""
    by = {f["name"]: f for f in flds}
    out = [(title, [by.pop(n) for n in names if n in by]) for title, names in spec]
    if by:
        out.append(("Advanced", list(by.values())))
    return [(t, fs) for t, fs in out if fs]
