"""Fixed failure labels. Labels never carry request, credential or upstream text."""


class AdapterFailure(RuntimeError):
    label = "adapter_failure"

    def __init__(self):
        super().__init__(self.label)


class DeadlineExpired(AdapterFailure):
    label = "deadline_expired"


class QueueTimeout(AdapterFailure):
    label = "queue_timeout"


class RequestTimeout(AdapterFailure):
    label = "request_timeout"


class UpstreamIncomplete(AdapterFailure):
    label = "upstream_incomplete"


class ModelMismatch(AdapterFailure):
    label = "model_mismatch"


class IsolationFailed(AdapterFailure):
    label = "isolation_failed"


class KeyInvalid(AdapterFailure):
    label = "key_invalid"


class InvalidModelOutput(AdapterFailure):
    label = "invalid_model_output"


class NativeProtocolError(AdapterFailure):
    label = "native_protocol_error"


class PromptTooLarge(AdapterFailure):
    label = "prompt_too_large"


def failure_label(exc):
    if isinstance(exc, AdapterFailure):
        return exc.label
    if isinstance(exc, TimeoutError):
        return DeadlineExpired.label
    return "upstream_error:" + type(exc).__name__


def error_code(label):
    # Codex does not retry context_length_exceeded and tells the user to start
    # a new task; every other code is treated as a retryable stream failure.
    return "context_length_exceeded" if label == PromptTooLarge.label else label
