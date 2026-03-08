import re

from firm.core.util import get_version


def test_get_version():
    assert re.match(r"\d+\.\d+\.\d+", get_version("firm"))
