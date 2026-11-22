class UnsupportedModel(RuntimeError):
    pass


def index_repository(repository):
    if repository == "first":
        raise ConnectionError("database connection lost")
    if repository == "second":
        raise UnsupportedModel("embedding model is unavailable")
