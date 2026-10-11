"""Local CLI account binding only; never authenticate a browser as its server."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from .config import project_path
from .research_operations import (
    _principal_ineligibility, active_entity_restriction, load_operations_inputs, operations_config,
)
from .research_runtime import AUTOMATED_ACTOR_PREFIXES


class LocalIdentityError(ValueError):
    pass


def current_windows_subject() -> str:
    """Read this CLI's primary token. Impersonated threads fail closed."""
    if os.name != "nt":
        raise LocalIdentityError("Windows local account verification is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenThreadToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if advapi.OpenThreadToken(kernel.GetCurrentThread(), 0x0008, True, ctypes.byref(token)):
        kernel.CloseHandle(token)
        raise LocalIdentityError("thread impersonation is not supported for local review")
    if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN is the only permitted fallback.
        raise LocalIdentityError("cannot verify the current thread token")
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise LocalIdentityError("cannot query the current process token")
    try:
        size = wintypes.DWORD()
        session = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 12, ctypes.byref(session), ctypes.sizeof(session), ctypes.byref(size)):
            raise LocalIdentityError("cannot verify token session")
        if session.value == 0:
            raise LocalIdentityError("service session cannot perform local human review")
        if advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size)) or ctypes.get_last_error() != 122:
            raise LocalIdentityError("cannot determine token user size")
        if not 0 < size.value <= 65536:
            raise LocalIdentityError("invalid token user size")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise LocalIdentityError("cannot read token user")

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

        sid = ctypes.cast(buffer, ctypes.POINTER(SidAndAttributes)).contents.sid
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise LocalIdentityError("cannot convert token user SID")
        try:
            subject = value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
        if not subject or not re.fullmatch(r"S-1-(?:\d+-)+\d+", subject):
            raise LocalIdentityError("invalid token subject")
        return subject
    finally:
        kernel.CloseHandle(token)


def _policy_inputs(config):
    cfg = operations_config(config)
    raw_config = config.get("research_operations") or {}
    if raw_config.get("enabled") is not True or raw_config.get("enforce_roster") is not True:
        raise LocalIdentityError("local summary review requires enforced research operations")
    paths = [project_path(cfg[key]) for key in ("roster_path", "restrictions_path")]
    try:
        before = [path.read_bytes() for path in paths]
        raw = json.loads(before[0].decode("utf-8-sig"))
        if not isinstance(raw, dict) or not isinstance(raw.get("principals"), list):
            raise LocalIdentityError("invalid identity roster")
        inputs = load_operations_inputs(config)
        if before != [path.read_bytes() for path in paths] or inputs["errors"]:
            raise LocalIdentityError("invalid or changing identity/restriction policy")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise LocalIdentityError("identity/restriction policy cannot be verified") from exc
    fingerprint = hashlib.sha256(json.dumps({
        "operations": cfg, "roster": hashlib.sha256(before[0]).hexdigest(),
        "restrictions": hashlib.sha256(before[1]).hexdigest(),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return raw, inputs, fingerprint


def local_actor_binding(config, actor, symbols, *, review=False):
    """Validate current policy binding, without asserting the caller's identity."""
    if not isinstance(actor, str) or not actor.strip() or actor.lower().startswith(AUTOMATED_ACTOR_PREFIXES):
        raise LocalIdentityError("a non-automated named human actor is required")
    raw, inputs, fingerprint = _policy_inputs(config)
    principal = inputs["principal_map"].get(actor)
    entries = [entry for entry in raw.get("principals", []) if isinstance(entry, dict)]
    source = next((entry for entry in entries if entry.get("actor_id") == actor), {})
    if not principal or source.get("active") is not True or principal["principal_type"] != "human":
        raise LocalIdentityError("actor must be an active human in the enforced roster")
    identity = source.get("identity") or {}
    if not isinstance(identity, dict):
        raise LocalIdentityError("invalid identity binding")
    subject = identity.get("subject")
    if (identity.get("provider") != "windows_local" or not isinstance(subject, str)
            or not re.fullmatch(r"S-1-(?:\d+-)+\d+", subject)
            or not isinstance(identity.get("bound_evidence_ref"), str)
            or not identity["bound_evidence_ref"].startswith("control:")):
        raise LocalIdentityError("actor has no recorded Windows identity binding")
    try:
        bound_at = datetime.fromisoformat(identity["bound_at"])
        if bound_at.tzinfo is None or bound_at > datetime.now(timezone.utc) + timedelta(seconds=120):
            raise ValueError("unverified binding time")
    except (ValueError, TypeError, KeyError) as exc:
        raise LocalIdentityError("identity binding requires a valid timezone-aware timestamp") from exc
    duplicate = [entry for entry in entries if isinstance(entry.get("identity"), dict)
                 and entry["identity"].get("provider") == "windows_local"
                 and entry["identity"].get("subject") == subject]
    if len(duplicate) != 1:
        raise LocalIdentityError("Windows subject is not uniquely bound")
    if review and "summary_review_authorized" not in principal["authorizations"]:
        raise LocalIdentityError("actor lacks explicit summary review authorization")
    if not isinstance(symbols, list) or not symbols or any(not isinstance(s, str) or not s for s in symbols):
        raise LocalIdentityError("summary review requires explicit symbol coverage")
    for symbol in symbols:
        reasons = _principal_ineligibility(
            principal, "information_analyst", {"entity": symbol},
            active_entity_restriction(symbol, inputs["restrictions"]), {}, enforce_capacity=False,
        )
        if reasons:
            raise LocalIdentityError("actor is not eligible for summary scope: " + "; ".join(reasons))
    return {"actor_id": actor, "provider": "windows_local", "subject": subject,
            "binding_evidence_ref": identity["bound_evidence_ref"], "policy_sha256": fingerprint,
            "assurance": "local_os_account_not_browser_identity_or_human_presence"}


def authenticate_local_actor(config, actor, symbols, *, review=False):
    binding = local_actor_binding(config, actor, symbols, review=review)
    if current_windows_subject() != binding["subject"]:
        raise LocalIdentityError("current Windows account does not match the claimed actor")
    return binding
