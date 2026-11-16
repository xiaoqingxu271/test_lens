def list_default(items=[]):
    return items


def dict_default(options={}):
    return options


def set_default(values=set()):
    return values


def bytearray_default(buffer=bytearray()):
    return buffer


def tuple_default(pair=([], "x")):
    return pair


def copied_default(items=[]):
    return list(items)


def required_arg(item, items=[]):
    items.append(item)
    return items


def outer():
    def nested(items=[]):
        return items

    return nested
