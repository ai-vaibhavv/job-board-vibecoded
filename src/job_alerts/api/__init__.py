"""FastAPI JSON layer over the Gradio-free dashboard service.

The React SPA in `frontend/` talks to this. Every route is a thin delegate to
`job_alerts.dashboard.service`; no data access or LLM/Discord logic lives here.
"""
