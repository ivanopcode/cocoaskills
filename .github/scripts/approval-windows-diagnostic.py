import sys

import pytest


def trace(frame, event, argument):
    if event == 'exception':
        kind, value, _ = argument
        if issubclass(kind, ValueError) and 'mount' in str(value):
            print('CROSS_DRIVE_EXCEPTION', frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name, str(value), file=sys.__stderr__, flush=True)
    return trace


sys.settrace(trace)
raise SystemExit(pytest.main(sys.argv[1:]))
