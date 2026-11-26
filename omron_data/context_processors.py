from records.contract import API_PREFIX

from .version import app_version

REPOSITORY_URL = "https://github.com/MartinWa/omron-data"


def app_info(request):
    return {
        "app_version": app_version(),
        "app_repository_url": REPOSITORY_URL,
        "api_docs_url": f"{API_PREFIX.rstrip('/')}/docs",
    }
