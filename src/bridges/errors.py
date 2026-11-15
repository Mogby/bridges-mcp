class BridgeError(Exception):
    """Safe, public error: never put upstream responses or secrets in this message."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def result(self):
        return {"code": self.code, "message": str(self), **self.details}


class OperationRejected(BridgeError):
    """A backend explicitly rejected a mutation; its outcome is known."""
