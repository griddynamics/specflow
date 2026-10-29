"""Fixtures for semgrep/fastapi.yml (vendored upstream pack):
positive and negative cases per rule, one section per rule id."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- kit.fastapi.no-debug-mode ---

# ruleid: kit.fastapi.no-debug-mode
app = FastAPI(debug=True)

# ruleid: kit.fastapi.no-debug-mode
app.debug = True

# ok: kit.fastapi.no-debug-mode
app = FastAPI(debug=settings.debug)


# --- kit.fastapi.no-credentialed-wildcard-cors ---

# ruleid: kit.fastapi.no-credentialed-wildcard-cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_credentials=True,
)

# ok: kit.fastapi.no-credentialed-wildcard-cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.example.com"],
    allow_methods=["*"],
    allow_credentials=True,
)
