"""cloudflare python worker entry (plain handler without fastapi), importable only inside workerd"""
from workers import WorkerEntrypoint, Response # type: ignore[import-not-found]
from syncapi.plain import handle


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        r = await handle(request.method, request.url, request.headers, self.env)
        body = r.stream.body if r.stream is not None else r.body
        return Response(body, status=r.status, headers=dict(r.headers))
