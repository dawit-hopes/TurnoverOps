class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: object | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def not_found() -> AppError:
    return AppError(404, "not_found", "Not found")
