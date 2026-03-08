import logging


class ServerException(Exception):
    def __init__(self, message: str, level: int = logging.ERROR) -> None:
        super().__init__(level, message)

    @property
    def level(self) -> int:
        return self.args[0]

    @property
    def message(self) -> str:
        return self.args[1]
