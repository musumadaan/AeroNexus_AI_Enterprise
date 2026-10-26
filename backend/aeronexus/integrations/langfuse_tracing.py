
"""
Optional Langfuse tracing for AeroNexus AI.

Privacy:
- Does not send prompts or model outputs.
- Records only observation names, model identifiers, provider,
  task, attempt number, and error type.
- Tracing is disabled when credentials are missing or the feature flag
  is disabled.
- Langfuse failures must never break application LLM calls.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


_FALSE_VALUES = {"0", "false", "off", "no"}


def _is_enabled() -> bool:
    """Return whether Langfuse tracing is configured and enabled."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    feature_flag = os.getenv("AERONEXUS_FF_LANGFUSE", "1").strip().lower()

    return bool(
        public_key
        and secret_key
        and feature_flag not in _FALSE_VALUES
    )


@contextmanager
def generation(
    *,
    name: str,
    model: str,
    provider: str,
    task: str,
    attempt: int,
) -> Iterator[Any]:
    """
    Create an optional Langfuse generation observation.

    The yielded value is the active Langfuse observation, or None when
    tracing is unavailable.

    Exceptions raised inside the caller's block are re-raised unchanged.
    Langfuse setup, annotation, and teardown failures are suppressed so
    that observability does not interfere with model execution.
    """
    if not _is_enabled():
        yield None
        return

    # Initialize Langfuse without allowing setup failures to affect the app.
    try:
        from langfuse import get_client

        client = get_client()
        observation_context = client.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            metadata={
                "provider": provider,
                "task": task,
                "attempt": attempt,
            },
        )
    except Exception:
        yield None
        return

    # Enter the observation separately so setup errors fail open.
    try:
        active = observation_context.__enter__()
    except Exception:
        yield None
        return

    caller_exception: BaseException | None = None
    caller_traceback = None

    try:
        try:
            yield active
        except BaseException as exc:
            # Remember caller exceptions so tracing cannot replace them.
            caller_exception = exc
            caller_traceback = exc.__traceback__

            # Best-effort error annotation; never mask the original error.
            try:
                active.update(
                    level="ERROR",
                    status_message=type(exc).__name__,
                )
            except Exception:
                pass
    finally:
        # Exit the observation. If the caller already raised, pass that
        # exception to Langfuse, but never let teardown mask the caller error.
        try:
            observation_context.__exit__(
                type(caller_exception) if caller_exception else None,
                caller_exception,
                caller_traceback,
            )
        except Exception:
            if caller_exception is None:
                # The model call itself succeeded; ignore tracing teardown errors.
                pass

    # Re-raise any exception originating from the caller's block.
    if caller_exception is not None:
        raise caller_exception.with_traceback(caller_traceback)
