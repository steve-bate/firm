import os

from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


async def sse_client_page_endpoint(request: Request) -> Response:
    return templates.TemplateResponse(
        "sse-client.jinja2",
        {
            "request": request,
        },
    )
