class Stub:
    def get(self, path, **kwargs):
        return lambda function: function


app = Stub()


@app.get("/classify")
def classify(x):
    if x > 0:
        return 1
    if x == 0:
        return 0
    return -1
