import contextlib


def mutable_default(items=[]):
    return items


def none_deref():
    x = None
    return x.value


def guarded_none():
    x = None
    if x is not None:
        return x.value
    return 0


def bare_except():
    try:
        return 1
    except:
        return 0


def try_finally_close():
    handle = open("data.txt")
    try:
        handle.read()
    finally:
        handle.close()


def exit_stack_managed():
    with contextlib.ExitStack() as stack:
        handle = stack.enter_context(open("data.txt"))
    return handle
