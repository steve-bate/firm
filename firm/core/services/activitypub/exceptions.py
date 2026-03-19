from typing import Any

from firm.core.services.exception import ServiceException


class NotAuthorizedException(ServiceException):
    def __init__(self, reason: str | None = None):
        super().__init__(reason or "Not authorized")

    @property
    def reason(self):
        return self.args[0]


class NotFoundException(ServiceException):
    def __init__(self, url: Any):
        super().__init__(str(url))

    @property
    def url(self):
        return self.args[0]


class InvalidRequestException(ServiceException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class InvalidResourceTypeException(InvalidRequestException):
    def __init__(self, actual_type: Any, expected_type: Any):
        super().__init__(f"Invalid resource type: {actual_type}, expected: {expected_type}")

    @property
    def actual_type(self):
        return self.args[0]

    @property
    def expected_type(self):
        return self.args[1]


class ResourceOwnerException(InvalidRequestException): ...


class InvalidResourceException(InvalidRequestException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
