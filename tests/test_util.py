import re

from firm.util import get_version


def test_get_version():
    assert re.match(r"\d+\.\d+\.\d+", get_version("firm"))
