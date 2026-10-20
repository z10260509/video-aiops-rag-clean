"""In-cluster connection settings shared by evidence and repair clients."""

import logging
import os
import ssl
from app.core.config import Settings

log = logging.getLogger(__name__)
_SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"


def kubernetes_connection(settings: Settings) -> tuple[str, str | None, ssl.SSLContext] | None:
    base_url = settings.kubernetes_api_url
    token = settings.kubernetes_bearer_token
    ca_file = settings.kubernetes_ca_file
    if not base_url:
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if host:
            base_url = f"https://{host}:{port}"
            default_ca = f"{_SERVICE_ACCOUNT}/ca.crt"
            ca_file = ca_file or (default_ca if os.path.isfile(default_ca) else None)
    if not token:
        token_path = f"{_SERVICE_ACCOUNT}/token"
        try:
            with open(token_path, encoding="utf-8") as token_stream:
                token = token_stream.read().strip()
        except OSError:
            token = None
    if not base_url or not token:
        return None
    try:
        context = ssl.create_default_context(cafile=ca_file if ca_file else None)
    except (OSError, ssl.SSLError):
        log.warning("Could not load configured Kubernetes CA bundle")
        return (base_url.rstrip("/"), token, ssl.create_default_context())
    return base_url.rstrip("/"), token, context
