from failure_source import UnsupportedModel, index_repository


def index_registered(repositories, retry):
    first_failure = None
    for repository in repositories:
        try:
            index_repository(repository)
        except UnsupportedModel:
            raise
        except Exception as error:
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        retry(first_failure)
