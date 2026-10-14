"""Exception locations without arguments, response bodies, URLs or secrets."""
from pathlib import PurePath


def exception_metadata(error: BaseException) -> dict[str, object]:
    result: dict[str, object] = {"exception_type": type(error).__name__}
    locations: list[str] = []
    trace = error.__traceback__
    while trace is not None:
        locations.append(
            f"{PurePath(trace.tb_frame.f_code.co_filename).name}:"
            f"{trace.tb_lineno}:{trace.tb_frame.f_code.co_name}"
        )
        trace = trace.tb_next
    result["frames"] = locations[-5:]
    cause = error.__cause__ or error.__context__
    if cause is not None:
        result["cause_type"] = type(cause).__name__
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        result["http_status"] = status
    return result
