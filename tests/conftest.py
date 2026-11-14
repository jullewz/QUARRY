import pytest
from oracle import Oracle

import quarry


@pytest.fixture(scope="session")
def oracle() -> Oracle:
    return Oracle()


@pytest.fixture
def db():
    con = quarry.connect(":memory:", autocommit=True)
    yield con
    con.close()
