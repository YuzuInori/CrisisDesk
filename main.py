import os
from backend.api.routes import app

if __name__ == "__main__":
    import uvicorn
    dev_mode = os.getenv("DEV_MODE", "0") == "1"
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=dev_mode)
