"""cloudflare python worker entry (fastapi + asgi), importable only inside workerd (`workers`, `asgi`)"""

from workers import WorkerEntrypoint # type: ignore[import-not-found]
import asgi # type: ignore[import-not-found]

from syncapi.app import create_app

APP = create_app()


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(APP, request, self.env)
