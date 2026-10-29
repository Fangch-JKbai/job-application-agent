"""Expected business errors; unexpected database failures remain visible."""


class ServiceError(Exception):
    status_code = 400


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class InvalidInput(ServiceError):
    status_code = 422
