import pytest

from lxask.route import specialist
from lxask.validate import build_catalog


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(["lsof", "ss", "tar", "sudo", "find"], [])


@pytest.mark.parametrize("question", [
    "which process listens on port 8080 without lsof",
    "list open ports, don't use ss",
    "install nginx without sudo",
    "set a proxy for apt",
    "add a proxy",
])
def test_ruled_out_programs_and_proxies_go_to_the_adapter(question, catalog):
    assert specialist(question, catalog)


@pytest.mark.parametrize("question", [
    "create a tar archive of a folder with no compression",
    "show the log without the header",
    "list the files without recursion",
    "rerun a command every 2 seconds",
    "edit proxy.conf",
])
def test_everything_else_stays_with_the_model(question, catalog):
    assert not specialist(question, catalog)


def test_without_a_catalogue_any_ruled_out_word_counts():
    assert specialist("show the log without the header")
