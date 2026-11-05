import tempfile
from pathlib import Path

import uvicorn

from app.main import Config, create_app
from app.schemas import Settings

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="qq-bot-ui-test-") as temporary:
        config = Config(db_path=Path(temporary) / "test.sqlite3")
        app = create_app(config)
        settings = app.state.store.settings().model_dump()
        app.state.store.update_settings(
            Settings.model_validate(
                {
                    **settings,
                    "models": [
                        {"id": "gpt-5.6-luna", "enabled": True},
                        {"id": "gpt-6.1-sol", "enabled": True},
                    ],
                    "default_model": "gpt-5.6-luna",
                }
            )
        )
        uvicorn.run(app, host="127.0.0.1", port=8766, access_log=False)
