class InvalidGenerationSessionStateError(Exception):
    """
    Raised when a caller attempts a state transition that is not allowed
    for the generation's current status.
    """
    def __init__(self, generation_id: str, current_status: str,
                 attempted_transition: str, allowed_from: list[str]):
        self.generation_id = generation_id
        self.current_status = current_status
        self.attempted_transition = attempted_transition
        self.allowed_from = allowed_from
        super().__init__(
            f"Cannot call '{attempted_transition}' on generation {generation_id}: "
            f"current status is '{current_status}', allowed from: {allowed_from}"
        )


class InvalidWorkspaceStateError(Exception):
    """
    Raised when a caller attempts a workspace state transition that is not allowed.
    """
    def __init__(self, workspace_id: str, current_status: str,
                 attempted_transition: str, allowed_from: list[str]):
        self.workspace_id = workspace_id
        self.current_status = current_status
        self.attempted_transition = attempted_transition
        self.allowed_from = allowed_from
        super().__init__(
            f"Cannot call '{attempted_transition}' on workspace {workspace_id}: "
            f"current status is '{current_status}', allowed from: {allowed_from}"
        )


class WorkspaceOwnershipError(Exception):
    """
    Raised when archive_and_release is called but locked_by != generation_id.
    Invariant 1 enforcement.
    """
    def __init__(self, workspace_id: str, expected_owner: str, actual_owner: str):
        super().__init__(
            f"Workspace {workspace_id} ownership check failed: "
            f"expected locked_by='{expected_owner}', got '{actual_owner}'. "
            f"Refusing to release workspace without confirmed ownership."
        )


class ArchivePreconditionError(Exception):
    """
    Raised when complete() is called but outputs_archived is not True.
    Invariant 7 enforcement.
    """
    pass


class CleanupTargetError(Exception):
    """
    Raised when a cleanup operation targets an ALLOCATED workspace.
    Invariant 3 and 4 enforcement.
    """
    pass



class GenerationCancelledError(Exception):
    """
    Raised by cooperative cancellation checks (``raise_if_cancelled``) when the
    session has been transitioned to CANCELLED while its workflow is still running.

    Deliberately an ``Exception`` (not ``BaseException``) so it propagates through the
    existing ``except Exception`` layers up to the shared workflow exception handler,
    where it is intercepted and routed to a silent no-op (no fail(), no notification —
    the session is already terminal).
    """
    def __init__(self, generation_id: str):
        self.generation_id = generation_id
        super().__init__(f"Generation {generation_id} was cancelled by user")


class MaxRetriesExceededError(Exception):
    """
    Raised by reset_for_retry when retry_count >= max_retries.
    Callers must present this error to the user rather than silently failing.
    """
    def __init__(self, generation_id: str, retry_count: int, max_retries: int):
        self.generation_id = generation_id
        self.retry_count = retry_count
        self.max_retries = max_retries
        super().__init__(
            f"Cannot retry generation {generation_id}: "
            f"retry_count ({retry_count}) has reached max_retries ({max_retries}). "
            f"Manual operator intervention required."
        )
