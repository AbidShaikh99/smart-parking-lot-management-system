# utils/response.py

from rest_framework.response import Response


def success_response(
    message="Request successful.",
    data=None,
    status_code=200,
):
    """
    Return a standard successful API response.
    """
    return Response(
        {
            "success": True,
            "message": message,
            "data": data,
        },
        status=status_code,
    )


def error_response(
    message="Something went wrong.",
    errors=None,
    status_code=400,
):
    """
    Return a standard error API response.
    """
    return Response(
        {
            "success": False,
            "message": message,
            "errors": errors,
        },
        status=status_code,
    )