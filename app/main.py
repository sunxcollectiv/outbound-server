from fastapi import FastAPI

from app.api import auth, areas, places, trip_items, trips, users

app = FastAPI(title="Outbound API", version="0.1.0")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(trips.router)
app.include_router(trip_items.router)
app.include_router(places.router)
app.include_router(areas.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
