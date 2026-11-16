class FlaskStub:
    def route(self, rule=None, **kwargs):
        return lambda function: function

    def get(self, rule=None, **kwargs):
        return lambda function: function


bp = FlaskStub()


@bp.route("/hello")
def hello():
    return "hi"


@bp.get("/bye")
def bye():
    return "bye"


@bp.route(rule="/kw")
def kw():
    return "kw"


@bp.get(path="/kw-get")
def kw_get():
    return "kw"
