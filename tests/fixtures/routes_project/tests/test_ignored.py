from app import app


@app.get("/ignored")
def ignored():
    return {}
