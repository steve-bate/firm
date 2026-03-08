class ServiceException(Exception):
    """Base class for exceptions raised by services."""

    ...


class MissingStore(ServiceException):
    def __init__(self):
        super().__init__("No store")
