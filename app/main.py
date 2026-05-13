"""FastAPI app entrypoint. Real wiring lands in Task 2 (middleware) and Task 6 (routes)."""

from fastapi import FastAPI

app = FastAPI(title="DC Inventory Backend", version="0.1.0")
