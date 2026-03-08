import asyncio
import functools
from typing import Any, Callable


# NOTE This must be the last decorator on the command
def async_command(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to run a Click command in an async context."""

    @functools.wraps(func)
    def wrapper(*args: tuple[Any], **kwargs: dict[str, Any]) -> Any:
        return asyncio.run(func(*args, **kwargs))

    return wrapper
