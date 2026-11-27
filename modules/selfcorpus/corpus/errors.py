class CorpusError(Exception):
    """A stable public error without internal paths, traces or payloads."""

    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
