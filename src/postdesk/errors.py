from __future__ import annotations


class PostdeskError(RuntimeError):
    exit_code = 2
    kind = "invalid_input"

    def __init__(self, detail: str, *, path: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.path = path


class InvalidInput(PostdeskError):
    pass


class ConfigError(PostdeskError):
    exit_code = 3
    kind = "config"


class ApiError(PostdeskError):
    exit_code = 4
    kind = "network_api"

    def __init__(
        self,
        detail: str,
        *,
        path: str = "",
        definitive: bool = False,
        not_found: bool = False,
    ) -> None:
        super().__init__(detail, path=path)
        self.definitive = definitive
        self.not_found = not_found


class ClaimConflict(PostdeskError):
    exit_code = 5
    kind = "claim_conflict"


def error_payload(exc: PostdeskError) -> dict:
    return {
        "ok": False,
        "error": {"kind": exc.kind, "detail": exc.detail, "path": exc.path},
    }
