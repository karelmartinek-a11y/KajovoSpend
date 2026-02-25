from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator

# Context proměnné – udržují se per-thread/async task.
correlation_id_var = contextvars.ContextVar("correlation_id", default=None)
document_id_var = contextvars.ContextVar("document_id", default=None)
file_sha256_var = contextvars.ContextVar("file_sha256", default=None)
job_id_var = contextvars.ContextVar("job_id", default=None)
phase_var = contextvars.ContextVar("phase", default=None)
attempt_var = contextvars.ContextVar("attempt", default=None)
mode_var = contextvars.ContextVar("mode", default=None)
openai_request_id_client_var = contextvars.ContextVar("openai_request_id_client", default=None)


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def get_forensic_fields() -> Dict[str, Any]:
    """Vrátí současný stav všech forenzních contextvars jako dict."""
    return {
        "correlation_id": correlation_id_var.get(),
        "document_id": document_id_var.get(),
        "file_sha256": file_sha256_var.get(),
        "job_id": job_id_var.get(),
        "phase": phase_var.get(),
        "attempt": attempt_var.get(),
        "mode": mode_var.get(),
        "openai_request_id_client": openai_request_id_client_var.get(),
    }


@contextmanager
def forensic_scope(**fields: Any) -> Iterator[None]:
    """
    Context manager, který dočasně nastaví vybrané contextvars.
    Po opuštění scope se původní hodnoty obnoví.
    """
    tokens: Dict[str, Any] = {}
    try:
        if "correlation_id" in fields:
            tokens["correlation_id"] = correlation_id_var.set(fields["correlation_id"])
        if "document_id" in fields:
            tokens["document_id"] = document_id_var.set(fields["document_id"])
        if "file_sha256" in fields:
            tokens["file_sha256"] = file_sha256_var.set(fields["file_sha256"])
        if "job_id" in fields:
            tokens["job_id"] = job_id_var.set(fields["job_id"])
        if "phase" in fields:
            tokens["phase"] = phase_var.set(fields["phase"])
        if "attempt" in fields:
            tokens["attempt"] = attempt_var.set(fields["attempt"])
        if "mode" in fields:
            tokens["mode"] = mode_var.set(fields["mode"])
        if "openai_request_id_client" in fields:
            tokens["openai_request_id_client"] = openai_request_id_client_var.set(fields["openai_request_id_client"])
        yield
    finally:
        for key, tok in tokens.items():
            try:
                if key == "correlation_id":
                    correlation_id_var.reset(tok)
                elif key == "document_id":
                    document_id_var.reset(tok)
                elif key == "file_sha256":
                    file_sha256_var.reset(tok)
                elif key == "job_id":
                    job_id_var.reset(tok)
                elif key == "phase":
                    phase_var.reset(tok)
                elif key == "attempt":
                    attempt_var.reset(tok)
                elif key == "mode":
                    mode_var.reset(tok)
                elif key == "openai_request_id_client":
                    openai_request_id_client_var.reset(tok)
            except Exception:
                # reset nesmí nikdy shodit volající kód
                pass
