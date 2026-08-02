"""TOOL-01: tp_auth_status - Check authentication status."""

from typing import Any

from tp_mcp.auth import AuthStatus, get_credential, get_storage_backend, validate_auth
from tp_mcp.client.context import cloud_credential, cloud_principal


async def tp_auth_status() -> dict[str, Any]:
    """Check TrainingPeaks authentication status.

    Returns:
        Dict with auth status, athlete_id if valid, and any action needed.
    """
    principal = cloud_principal.get()
    if principal is not None:
        cookie = cloud_credential.get()
        storage = "request-header"
        authenticate_action = "Send a current TrainingPeaks cookie in X-TrainingPeaks-Auth"
    else:
        cred = get_credential()
        cookie = cred.cookie if cred.success else None
        storage = get_storage_backend()
        authenticate_action = "Run 'tp-mcp auth' to authenticate"

    if not cookie:
        return {
            "valid": False,
            "athlete_id": None,
            "message": "No TrainingPeaks credential supplied" if principal is not None else "No credential stored",
            "action_needed": authenticate_action,
        }

    result = await validate_auth(cookie)

    if result.is_valid:
        return {
            "valid": True,
            "athlete_id": result.athlete_id,
            "email": result.email,
            "storage": storage,
            "message": "Authentication valid",
            "action_needed": None,
        }

    local_or_cloud_reauth = (
        "Send a fresh X-TrainingPeaks-Auth header."
        if principal is not None
        else "Run 'tp-mcp auth' to re-authenticate."
    )
    action_map = {
        AuthStatus.EXPIRED: f"Session expired. {local_or_cloud_reauth}",
        AuthStatus.INVALID: f"Invalid credentials. {local_or_cloud_reauth}",
        AuthStatus.NETWORK_ERROR: "Network error. Check connection and retry.",
    }

    return {
        "valid": False,
        "athlete_id": None,
        "message": result.message,
        "action_needed": action_map.get(result.status, authenticate_action),
    }
