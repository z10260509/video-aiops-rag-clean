import hmac

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


def authenticate(kind, credentials, settings):
    operator = settings.aiops_operator_token.get_secret_value()
    webhook = settings.aiops_webhook_token.get_secret_value()
    if not settings.aiops_enabled or min(len(operator), len(webhook)) < 32 or operator == webhook:
        raise HTTPException(503, "AIOps authentication is not configured")
    expected = operator if kind == "operator" else webhook
    if (
        not credentials
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials.encode(), expected.encode())
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return settings.aiops_operator_principal if kind == "operator" else "alertmanager"


def operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
):
    return authenticate("operator", credentials, settings)


def webhook_sender(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
):
    return authenticate("webhook", credentials, settings)
