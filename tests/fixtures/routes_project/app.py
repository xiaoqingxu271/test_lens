class Stub:
    def get(self, path, **kwargs):
        return lambda function: function

    def post(self, path, **kwargs):
        return lambda function: function

    def route(self, path, **kwargs):
        return lambda function: function

    def api_route(self, path, **kwargs):
        return lambda function: function

    def websocket(self, path, **kwargs):
        return lambda function: function

    def errorhandler(self, code):
        return lambda function: function


app = Stub()
router = Stub()


@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {"item_id": item_id}


@router.post("/items")
async def create_item():
    return {}


@app.api_route("/multi", methods=["GET", "POST"])
def multi():
    return {}


@app.route("/flask", methods=["GET", "POST"])
def flask_route():
    return {}


@app.get("/first")
@app.get("/second")
def two_paths():
    return {}


def helper():
    return 42


@app.errorhandler(404)
def not_found(error):
    return {}


@app.websocket("/ws")
def websocket_route():
    return {}


DYNAMIC = "/dynamic"


@app.get(DYNAMIC)
def dynamic_path():
    return {}


@app.get()
def missing_path():
    return {}


@app.route("/dynamic-methods", methods=["GET", DYNAMIC])
def dynamic_methods():
    return {}
