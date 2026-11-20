from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings.
    Data Flow: env vars / .env -> Settings -> imported by db, auth, llm, agent."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///agent.db"  # Postgres in Docker
    checkpoint_db: str = "checkpoints.db"     # LangGraph saved state
    app_tz: str = "Asia/Kolkata"              # timezone of cab times + cut-offs (not the server's)
    jwt_secret: str = "change-me-to-a-32-byte-secret!!!"
    llm_provider: str = "fake"  # fake | groq | gemini
    llm_api_key: str = ""
    llm_model: str = ""          # empty = provider default (see llm/client.py)
    llm_timeout_s: float = 2.0   # one LLM call, wall clock
    llm_budget_s: float = 3.5    # all LLM calls of one message together (p95 < 5 s)
    llm_max_tokens: int = 300    # short outputs = fast outputs (room for a little hidden reasoning)
    demo_mode: bool = True  # enables /demo/login for the UI; set False in production


settings = Settings()
