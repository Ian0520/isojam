import pytest
import app.jobs as jobs
import app.uploads as uploads

@pytest.fixture(autouse=True)
def clear_registries():
    jobs.jobs.clear()
    uploads.uploads.clear()