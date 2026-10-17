from turnoverops.app import create_app

app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("turnoverops.main:app", host="0.0.0.0", port=8000, reload=False)
